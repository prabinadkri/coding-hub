"""Local Python AST map. Relations are syntactic hints, never resolved call graphs."""
import ast
import re


def extract(source):
    try: tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError): return []
    result = []
    class Visitor(ast.NodeVisitor):
        scope = []
        def definition(self, node, kind):
            name = '.'.join(self.scope + [node.name])
            signature = node.name
            if hasattr(node, 'args'):
                signature += '(' + ast.unparse(node.args)[:260] + ')'
            result.append((name, kind, node.lineno, getattr(node, 'end_lineno', node.lineno), signature))
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()
        def visit_FunctionDef(self, node): self.definition(node, 'function')
        def visit_AsyncFunctionDef(self, node): self.definition(node, 'async function')
        def visit_ClassDef(self, node): self.definition(node, 'class')
        def visit_Call(self, node):
            name = ast.unparse(node.func)[:180]
            result.append((name, 'call', node.lineno, node.lineno, '.'.join(self.scope) or '<module>'))
            self.generic_visit(node)
        def visit_Import(self, node):
            for item in node.names:
                result.append((item.name, 'import', node.lineno, node.lineno, item.asname or item.name))
        def visit_ImportFrom(self, node):
            for item in node.names:
                result.append(((node.module or '') + '.' + item.name, 'import', node.lineno, node.lineno, item.asname or item.name))
    try: Visitor().visit(tree)
    except RecursionError: return []
    return result[:3000]


def search(db, query, limit=18):
    terms = list(dict.fromkeys(re.findall(r'[A-Za-z_][A-Za-z_0-9]{2,}', query)))[:16]
    if not terms: return []
    where = ' OR '.join('lower(name) LIKE ? ESCAPE "!"' for _ in terms)
    params = ['%' + t.lower().replace('!', '!!').replace('_', '!_') + '%' for t in terms]
    return [dict(r) for r in db.execute('SELECT path,name,kind,line,end_line,detail FROM symbols WHERE ' + where +
        " ORDER BY CASE WHEN kind IN ('function','class','async function') THEN 0 ELSE 1 END, path, line LIMIT ?", params + [min(limit, 40)])]
