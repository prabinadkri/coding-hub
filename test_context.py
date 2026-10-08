import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import hub
from context_engine import ProjectMemory
import quota


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.state = patch.object(hub, 'STATE', self.root / 'state')
        self.state.start()
        self.memory = ProjectMemory(self.project)

    def tearDown(self):
        self.state.stop()
        self.temp.cleanup()

    def test_delete_chat_removes_local_history_but_preserves_project_memory(self):
        from context_engine import project_tree
        source = self.project / 'billing.py'
        source.write_text('def billing_total(): return 42\n')
        self.memory.index()
        self.memory.remember('Keep the public API stable.')
        self.memory.initialize_rules()
        deleted = self.memory.conversation(goal='Delete me')
        kept = self.memory.conversation(goal='Keep me')
        self.memory.record(deleted, 'removed-run', 'privateword', 'privateword answer', 'completed')
        self.memory.record(kept, 'kept-run', 'Keep this', 'Preserved answer', 'completed')
        for task, chat in [('removed-run', deleted), ('kept-run', kept), ('interrupted-run', deleted)]:
            folder = hub.private_dir(hub.STATE / 'tasks' / task)
            hub.save_json(folder / 'context.json', {'conversation': chat})
            (folder / 'changes.json').write_text('{}')
        dashboard = hub.private_dir(hub.STATE / 'dashboard' / 'tasks')
        hub.save_json(dashboard / 'old-task.json', {'project': str(self.project.resolve()), 'conversation': deleted, 'status': 'completed'})
        (dashboard / 'old-task.log').write_text('privateword')
        result = self.memory.delete_conversation(deleted)
        self.assertEqual(result['task_ids'], ['old-task'])
        self.assertFalse(list(dashboard.iterdir()))
        self.assertFalse((hub.STATE / 'tasks' / 'removed-run').exists())
        self.assertFalse((hub.STATE / 'tasks' / 'interrupted-run').exists())
        self.assertTrue((hub.STATE / 'tasks' / 'kept-run' / 'changes.json').exists())
        self.assertFalse((self.memory.directory / 'checkpoints' / 'removed-run.json').exists())
        self.assertEqual(self.memory.messages(kept)['turns'][0]['result'], 'Preserved answer')
        self.assertEqual(self.memory.notes(), 'Keep the public API stable.')
        self.assertTrue((self.project / 'CODING_HUB.md').exists())
        self.assertTrue(self.memory.search('billing'))
        self.assertEqual(source.read_text(), 'def billing_total(): return 42\n')
        with self.memory.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM turns WHERE turns MATCH ?', ('privateword',)).fetchone()[0], 0)
        self.assertEqual([c['id'] for c in project_tree()[0]['conversations']], [kept])
        with self.assertRaises(ValueError):
            self.memory.record(deleted, 'late-run', 'late', 'late', 'completed')
        with self.assertRaises(ValueError):
            self.memory.delete_conversation(deleted)

    def test_delete_refuses_busy_workspace_invalid_ids_and_other_project(self):
        chat = self.memory.conversation(goal='Keep while running')
        with hub.project_lock(self.project), self.assertRaises(RuntimeError):
            self.memory.delete_conversation(chat)
        directory = hub.private_dir(hub.STATE / 'dashboard' / 'tasks')
        for status in ('queued', 'running', 'stopping'):
            hub.save_json(directory / 'active.json', {'project': str(self.project.resolve()), 'conversation': chat, 'status': status})
            with self.assertRaises(RuntimeError):
                self.memory.delete_conversation(chat)
        other = self.root / 'other'; other.mkdir()
        for identifier in (None, '../outside', 'a' * 32):
            with self.assertRaises(ValueError):
                self.memory.delete_conversation(identifier)
        with self.assertRaises(ValueError):
            ProjectMemory(other).delete_conversation(chat)
        self.assertEqual(self.memory.messages(chat)['goal'], 'Keep while running')

    def test_delete_never_follows_task_symlinks_or_traversal(self):
        chat = self.memory.conversation(goal='Remove')
        outside = self.root / 'outside'; outside.mkdir()
        (outside / 'keep.txt').write_text('keep')
        task_root = hub.private_dir(hub.STATE / 'tasks')
        (task_root / 'linked-task').symlink_to(outside, target_is_directory=True)
        self.memory.record(chat, 'linked-task', 'q', 'a', 'completed')
        with self.memory.connect() as db:
            db.execute('INSERT INTO turns VALUES(?,?,?,?,?,?)', (chat, '../../outside', 'q', 'a', 'completed', 0))
        self.memory.delete_conversation(chat)
        self.assertEqual((outside / 'keep.txt').read_text(), 'keep')
        self.assertFalse((task_root / 'linked-task').is_symlink())

    def test_new_projects_and_chats_sort_first_despite_old_clock_skew(self):
        from context_engine import project_tree
        first = self.memory.conversation(goal='Old chat')
        with self.memory.connect() as db:
            db.execute('UPDATE conversations SET updated=?', (9999999999,))
        second = self.memory.conversation(goal='New chat')
        other = self.root / 'new-project'; other.mkdir()
        memory = ProjectMemory(other); memory.conversation(goal='Start here')
        projects = project_tree()
        self.assertEqual(projects[0]['project'], str(other.resolve()))
        existing = next(p for p in projects if p['project'] == str(self.project.resolve()))
        self.assertEqual([c['id'] for c in existing['conversations']], [second, first])

    def test_old_replies_recover_assistant_text_without_rewriting_history(self):
        chat = self.memory.conversation(goal='Review code')
        original = '[tool] read · completed\nsource code\nThe answer is 42.'
        self.memory.record(chat, 'old-task', 'Review code', original, 'completed')
        log = hub.private_dir(hub.STATE / 'tasks' / 'old-task') / '1-local.log'
        log.write_text(json.dumps({'type':'tool_use','part':{'tool':'read','state':{'output':'source code'}}})+'\n'+json.dumps({'type':'text','part':{'text':'The answer is 42.'}})+'\n')
        self.assertEqual(self.memory.messages(chat)['turns'][0]['result'], 'The answer is 42.')
        with self.memory.connect() as db:
            self.assertEqual(db.execute('SELECT result FROM turns').fetchone()[0], original)

    def test_legacy_future_timestamps_do_not_pin_old_projects_on_top(self):
        from context_engine import project_tree
        self.memory.conversation(goal='Old clock')
        with self.memory.connect() as db:
            db.execute('UPDATE conversations SET updated=?', (9999999999,))
        other = self.root / 'recent'; other.mkdir()
        recent = ProjectMemory(other); recent.conversation(goal='Correct clock')
        for memory in (self.memory, recent):
            path = memory.directory / 'project.json'
            metadata = json.loads(path.read_text()); metadata.pop('last_activity')
            path.write_text(json.dumps(metadata))
        self.assertEqual(project_tree()[0]['project'], str(other.resolve()))
        with self.memory.connect() as db:
            self.assertEqual(db.execute('SELECT updated FROM conversations').fetchone()[0], 9999999999)

    def test_incremental_index_finds_symbols_and_updates_changed_and_deleted_files(self):
        path = self.project / 'module.py'
        path.write_text('def validate_checkout_total(amount):\n    return amount >= 0\n')
        self.assertEqual(self.memory.index()['updated_files'], 1)
        self.assertEqual(self.memory.search('validate checkout total')[0]['path'], 'module.py')
        self.assertEqual(self.memory.index()['updated_files'], 0)
        path.write_text('def calculate_refund(value):\n    return value\n')
        self.memory.index()
        self.assertEqual(self.memory.search('validate checkout'), [])
        self.assertEqual(len(self.memory.search('calculate refund')), 1)
        path.unlink()
        self.memory.index()
        self.assertEqual(self.memory.search('calculate refund'), [])

    def test_secrets_dependencies_and_external_symlinks_are_excluded(self):
        (self.project / '.env').write_text('password=PRIVATE')
        (self.project / 'credentials.json').write_text('PRIVATE')
        modules = self.project / 'node_modules'
        modules.mkdir()
        (modules / 'dependency.js').write_text('PRIVATE')
        outside = self.root / 'outside.py'
        outside.write_text('PRIVATE')
        (self.project / 'linked.py').symlink_to(outside)
        self.memory.index()
        self.assertEqual(self.memory.search('PRIVATE'), [])

    def test_long_conversation_is_bounded_and_pinned_requirements_survive(self):
        requirement = 'Never change the database schema.\n' + 'नेपाल ' * 100
        self.memory.remember(requirement)
        conversation = self.memory.conversation(goal='Build the billing system')
        for number in range(120):
            self.memory.record(conversation, f'task{number}', f'Work on feature {number}', 'Result: ' + 'large output ' * 500, 'completed')
        self.memory.record(conversation, 'decision', 'Use PostgreSQL for invoicing', 'Selected PostgreSQL; run migration tests next.', 'completed')
        context = self.memory.context(conversation, 'continue invoicing', budget=6000)
        self.assertLessEqual(len(context.encode()), 6000)
        self.assertIn(requirement, context)
        self.assertIn('PostgreSQL', context)
        self.assertEqual(self.memory.info()['turns'], 121)
        latest = self.memory.messages(conversation)
        earlier = self.memory.messages(conversation, before=latest['oldest_cursor'])
        self.assertEqual(len(latest['turns']), 30)
        self.assertTrue(latest['has_older'])
        self.assertLess(earlier['turns'][-1]['rowid'], latest['turns'][0]['rowid'])
        self.assertEqual(self.memory.conversation(resume=True), conversation)
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaises(ValueError):
            ProjectMemory(other).conversation(conversation)

    def test_pin_length_is_rejected_without_replacing_existing_requirements(self):
        self.memory.remember('Keep public APIs stable.')
        with self.assertRaises(ValueError):
            self.memory.remember('x' * 4001)
        self.assertEqual(self.memory.notes(), 'Keep public APIs stable.')

    def test_project_instruction_files_are_loaded_and_never_overwritten(self):
        (self.project / 'CLAUDE.md').write_text('Use pytest and keep the public API stable.')
        (self.project / 'AGENTS.md').write_text('Only change the requested module.')
        self.assertTrue(self.memory.initialize_rules()['created'])
        path = self.project / 'CODING_HUB.md'
        path.write_text('Custom user rules.')
        self.assertFalse(self.memory.initialize_rules()['created'])
        self.assertEqual(path.read_text(), 'Custom user rules.')
        conversation = self.memory.conversation(goal='Improve tests')
        context = self.memory.context(conversation, 'Improve tests')
        self.assertIn('Custom user rules.', context)
        self.assertIn('Use pytest', context)
        self.assertIn('Only change the requested module.', context)

    def test_saved_messages_remain_ordered_and_projects_are_grouped(self):
        from context_engine import project_tree
        conversation = self.memory.conversation(goal='A saved chat')
        self.memory.record(conversation, 'first', 'First question', 'First answer', 'completed')
        self.memory.record(conversation, 'second', 'Follow-up', 'Second answer', 'completed')
        detail = self.memory.messages(conversation)
        self.assertEqual([turn['request'] for turn in detail['turns']], ['First question', 'Follow-up'])
        self.assertEqual(detail['total_turns'], 2)
        tree = project_tree()
        self.assertEqual(tree[0]['project'], str(self.project.resolve()))
        self.assertEqual(tree[0]['conversations'][0]['id'], conversation)

    def test_index_continues_after_a_byte_budget_and_context_stays_small(self):
        for number in range(100):
            (self.project / f'module_{number}.py').write_text(f'def feature_{number}():\n    return "inventory_checkout"\n' * 5)
        first = self.memory.index(byte_budget=1000)
        self.assertFalse(first['complete'])
        second = self.memory.index(byte_budget=1000)
        self.assertGreater(second['indexed_files'], first['indexed_files'])
        full = self.memory.index(seconds=30)
        self.assertEqual(full['indexed_files'], 100)
        conversation = self.memory.conversation(goal='Inspect checkout')
        self.assertLessEqual(len(self.memory.context(conversation, 'inventory checkout').encode()), 12500)


class QuotaTests(unittest.TestCase):
    def test_official_quota_payload_is_sanitized_and_missing_reset_is_unknown(self):
        payload = {'status': 'SUCCESS', 'email': 'private@example.com', 'command': {'name': 'usage', 'data': {'groups': [
            {'name': 'Gemini', 'buckets': [{'id': 'weekly', 'remaining_fraction': .42, 'reset_time': '2030-01-01T00:00:00Z'},
                                          {'id': 'other', 'remaining_fraction': 1}, {'id': 'bad', 'remaining_fraction': 2}]}]}}}
        result = quota.parse_usage(payload)
        self.assertEqual(result['groups'][0]['buckets'][0]['remaining_percent'], 42)
        self.assertIsNone(result['groups'][0]['buckets'][1]['reset_at'])
        self.assertEqual(len(result['groups'][0]['buckets']), 2)
        self.assertNotIn('private@example.com', json.dumps(result))
        with self.assertRaises(ValueError):
            quota.parse_usage({'status': 'SUCCESS', 'response': 'quota is probably 90%'})


if __name__ == '__main__':
    unittest.main()
