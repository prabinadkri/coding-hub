# Smart route: paired coding check

Validated on 8 October 2026 with Antigravity CLI 1.3.1 and OpenCode 1.18.35.
Both runs used `gemini-3.8-flash-medium`; the Smart worker used `space-bunny-free`.
Each run started in a separate Git repository containing identical committed files.

## Task and starting code

```python
def calculate_checkout_total(items):
    return round(sum(items) * 1.13, 2)
```

The starting unittest checked `[5, 7] == 13.56`. Project instructions specified NPR, preservation of the public function name, and Python standard-library unittest.

Both routes received this same request with edits enabled:

> Update calculate_checkout_total in charges.py to reject any negative amount with ValueError. Preserve existing positive totals and return 0 for an empty list. Add unittest coverage for positive, empty, negative, and zero amounts. Run the tests and report the result.

## Results

| Measurement | Smart | Direct Antigravity |
| --- | ---: | ---: |
| Reported Antigravity total tokens | 8,171 | 122,733 |
| Manager invocations | 2 | Not applicable |
| Resulting project tests passed | 4 | 4 |
| Independent acceptance cases passed | 8 | 8 |

Smart's Antigravity token count was 93.3% lower for this task. It includes planning and review, using the dedicated tool-free manager. Direct used the ordinary native coding agent. Counts come from each native CLI response's `usage.total_tokens`; Smart sums both responses. Free worker tokens are not included in this Antigravity-only measurement.

Independent acceptance checks covered `[]`, `[0]`, `[5, 7]`, `[10, 0, 5]`, `[1.25, 2.5]`, `[-1]`, `[5, -7]`, and `[0, -0.01]`. Both modified only the requested implementation and test file. Smart recovered from a missing `python` executable by running `python3`.

## Limits

This is one small, controlled example. Passing these checks does not establish equal quality across codebases. Results depend on provider versions, caching, task size, evidence selection, and worker behavior. Smart can use more Antigravity tokens than a simple direct response. The production route does not run a second implementation merely to obtain a comparison. Normal task results therefore label direct-route savings as unmeasured.
