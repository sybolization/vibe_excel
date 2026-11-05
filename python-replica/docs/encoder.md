# Encoder

The independent, standard-library-only encoder turns an existing run directory
into an evaluable case. It does not parse Excel or call Node, TypeScript, or the
original project at runtime.

From `python-replica`, use the uv-managed environment:

```powershell
uv run vibe-encode claims add --file claims.jsonl --id c1 --kind alias `
  --subject 部门 --value "HRBP→人力资源业务伙伴" --evidence "timeline#12"
uv run vibe-encode claims confirm --file claims.jsonl --id c1 --by jinny --note "对"
uv run vibe-encode claims reject --file claims.jsonl --id c1 --by jinny --reason "旧口径"
uv run vibe-encode claims show --file claims.jsonl

uv run vibe-encode record --run .\runs\author-01 --id import-01 --family import `
  --question "把 HR 导出表建成 hr.db" --corpus .\corpus --claims claims.jsonl `
  --outcome partial --score 0.8 --category incomplete --note "外部核对结果"
uv run vibe-encode list --corpus .\corpus
uv run vibe-encode digest --corpus .\corpus
```

`uv run python -m vibe_excel.encoder` supports the same commands. `--corpus`
is required for `record`, `list`, and `digest`. Repeated `--evidence` flags collect
multiple pointers; other repeated flags use their last value.

## Python API

```python
from vibe_excel.encoder import (
    BuildTraceOptions, TraceOutcome, build_trace, fold_claims, parse_claims,
    save_trace, trace_hash,
)

claims = fold_claims(parse_claims(
    '{"op":"claim","id":"c1","kind":"unit","subject":"金额","value":"元"}'
))
trace = build_trace(BuildTraceOptions(
    run_dir=r".\runs\author-01",
    id="import-01",
    family="import",
    question="把表入库",
    outcome=TraceOutcome(label="partial"),
    claims=claims,
))
result = save_trace(r".\corpus", trace)
payload = trace.to_dict()  # External JSON retains camelCase field names.
fingerprint = trace_hash(trace)
```

APIs and dataclass attributes are snake_case. `InteractionTrace.from_dict()`
and `read_trace()` restore typed records from the external JSON schema.
`extract_run()` exposes steps, message counts, token/cost totals, bad-line
counts, and event counts. `run_cli(argv: list[str]) -> int` returns a status
without terminating its caller; the console entry point is
`vibe_excel.encoder.cli:main`.

## Preserved rules

- Pair tools by `toolCallId`. Unmatched ends remain visible; unfinished starts
  become failed/incomplete steps. An absent structured `exit_code` stays absent.
  `isError` and exit status are separate facts.
- Argument hashes sort object keys recursively; previews collapse whitespace and
  truncate at 220 UTF-16 code units. Costs are rounded to six decimal places.
- Claims are append-only. Each ID can be claimed once; repeated confirmations
  retain history. Rejected IDs cannot be confirmed again: reject the old claim
  and create a **new ID**. Evidence accepts a string or string array.
- Source/policy snapshots come from `author.json`, `verify.json`, or `run.json`,
  in that precedence order. Verify directories need no event stream. Only
  top-level files become hashed artifacts; nested business databases are excluded.
  Prompt hashes reflect the prompt's current content, or an empty hash if missing.
- Outcomes are `pass`, `partial`, or `fail`, with an optional external score in
  `0..1` and failure category. Neither tool success nor `verify.identical`
  infers an outcome. The CLI retains the original conservative `partial` default.
- Cases are immutable: `cases\<id>\trace.json` is never overwritten. Same ID/hash
  is unchanged even with a different `recordedAt`; changed content needs a new ID.
  `index.jsonl` only appends. Its digest depends exclusively on sorted ID/hash
  pairs; malformed index lines report their physical line number.

Claim kinds: `alias`, `field-meaning`, `measure-definition`, `join`, `filter`,
`unit`, `exception`. Failure categories: `schema-misread`, `alias-ambiguity`,
`join-wrong`, `type-coercion`, `unit-confusion`, `filter-rule`, `tooling-error`,
`incomplete`, `other`.

Focused validation: `uv run pytest tests\test_encoder.py`.
