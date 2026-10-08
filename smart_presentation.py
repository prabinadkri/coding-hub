"""Separate a Smart answer from its optional, locally saved run details."""
import json
import re


def attention(review):
    if not review or review.get('verdict') == 'pass':
        return ''
    text = 'Needs review: ' + str(review.get('summary') or review.get('verdict', 'Incomplete validation.'))
    if review.get('next_steps'):
        text += '\nNext steps: ' + str(review['next_steps'])
    return text + '\nChanges are preserved. Review these findings before continuing.'


def presentation(text, report):
    """Old saved replies stay untouched on disk; strip only known generated wrappers."""
    if isinstance(report.get('reply'), str):
        answer = report['reply']
    else:
        answer = re.sub(r'\n\nSmart: \d+/2 manager calls · reported manager tokens: (?:\d+|unavailable)\nSavings versus direct Antigravity: unmeasured\.$', '', text)
        review = report.get('review')
        if isinstance(review, dict):
            suffix = '\n\nManager review: ' + str(review.get('verdict', 'unknown')) + '\n' + str(review.get('summary', ''))
            if review.get('next_steps'):
                suffix += '\nNext steps: ' + str(review['next_steps'])
            if review.get('verdict') != 'pass':
                suffix += '\nManager call limit reached. Changes are preserved; continue with a focused follow-up after reviewing these findings.'
            if answer.endswith(suffix):
                answer = answer[:-len(suffix)]
                notice = attention(review)
                if notice:
                    answer += '\n\n' + notice
        if report.get('assignments'):
            answer = re.sub(r'^Integrated \d+ files from \d+ workers\. Integration checks follow\.\n\n', '', answer)
    return answer, details(report)


def details(report):
    review = report.get('review')
    lines = ['## Manager review']
    if isinstance(review, dict):
        lines += [str(review.get('verdict', 'Unknown')).capitalize(), str(review.get('summary', ''))]
        if review.get('next_steps'):
            lines += ['Next steps: ' + str(review['next_steps'])]
    else:
        lines += ['No completed review was saved.']
    lines += ['\n## Workers', 'Concurrent workers: ' + str(report.get('parallel_workers', 1))]
    for assignment in report.get('assignments', [])[:3]:
        lines += ['- Worker ' + str(assignment.get('number', '?')) + ': ' + str(assignment.get('goal', ''))[:1200] + ' · ' + str(assignment.get('status', 'unknown'))]
    for number, attempt in enumerate(report.get('worker_attempts', [])[:30], 1):
        lines += [f"- Attempt {number}: {attempt.get('backend', '?')} · {attempt.get('model', '?')} · " + str(attempt.get('failure') or 'completed')]
    if report.get('parallel_fallback'):
        lines += [str(report['parallel_fallback'])]
    amount = report.get('manager_total_tokens')
    lines += ['\n## Usage', 'Manager: ' + str(report.get('manager_model', 'Unknown')),
              f"Manager calls: {report.get('manager_calls', 0)}/{report.get('manager_call_limit', 2)}",
              'Reported manager tokens: ' + (str(amount) if amount is not None else 'Unavailable'),
              'Savings versus direct Antigravity: unmeasured. No direct baseline was run; token savings and equal quality are not guaranteed.']
    return '\n'.join(lines)[:14000]


def read(folder, text):
    path = folder / 'smart.json'
    try:
        if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            return text, ''
        report = json.loads(path.read_text())
        if not isinstance(report, dict):
            return text, ''
        return presentation(text, report)
    except (OSError, ValueError, TypeError, AttributeError):
        return text, ''
