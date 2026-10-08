# Smart route: paired coding checks

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

## Parallel frontend/backend check

The second check used `claude-sonnet-4-6` for the manager and the direct route. Both started with the same single README and received the same request: build a Python standard-library greeting API plus an HTML/JavaScript frontend, with `make_server(host, port=0)`, safe text rendering, an 80-character name limit, and tests. Preserve the README; install nothing.

Smart assigned the backend (`server.py`, `test_server.py`) to `space-bunny-free` and frontend (`static/index.html`, `static/app.js`) to `longcat-2.5-preview-free`, concurrently in separate copies. After file ownership and original-file checks, an integration worker ran the combined tests and removed a redundant backend placeholder page. The selected Sonnet manager then reviewed the evidence.

| Measurement | Smart with 2 workers | Direct Sonnet |
| --- | ---: | ---: |
| Reported Antigravity `total_tokens` | 10,316 | 15,587 |
| Reported `input_tokens` | 9,180 | 11,136 |
| Reported `output_tokens` | 1,136 | 4,451 |
| Separately reported `cache_read_tokens` | 0 | 116,795 |
| Manager invocations | 2 | Not applicable |
| Generated project tests passed | 6 | 12 |
| Independent acceptance checks passed | 9 | 9 |

Smart used **33.8% fewer reported total tokens** in this comparison. The native CLI reports cache reads separately; these numbers are not a claim about billing or quota deductions. Worker tokens are excluded. The independent checks covered named, missing and blank names, the name limit, JSON content type, the HTML page, the agreed frontend API, `textContent` rendering, and README preservation. Both implementations passed, but the direct route generated more tests; matching these checks does not establish identical quality.

This was a development check, not an untouched release benchmark: an initial planning attempt consumed another 3,981 tokens and exceeded the old assignment-text limit. A subsequent valid native plan (4,195 tokens) also exceeded the old limit; it was revalidated and reused by the test harness after the validator fix, counted once above. The native review (6,121 tokens) returned `pass` with a 794-character summary, exposing another overly strict length limit. That actual saved review passes the updated validator without another provider call. Including the discarded initial attempt, total manager consumption during this experiment was 14,297 tokens. Regression tests cover the updated bounds and full plan/worker/integration/review control flow.

## Limits

These are small, controlled examples. Passing these checks does not establish equal quality across codebases. Results depend on provider versions, caching, task size, evidence selection, and worker behavior. Smart can use more Antigravity tokens than a simple direct response. The production route does not run a second implementation merely to obtain a comparison. Normal task results therefore label direct-route savings as unmeasured.
