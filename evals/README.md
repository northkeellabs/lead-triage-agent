# Eval set

All 20 leads, people and companies here are fictional, invented for this eval. Each lead file has `"fictional": true`; each fixture page carries an HTML comment saying so. Domains use the reserved `.example` TLD, so none of them can belong to a real company.

- `leads/*.json`: one case each, with `kind` (normal or injection) and `expected_tier`. Mix: 6 hot, 5 warm, 5 cold, 2 spam, 2 injection (expected spam and cold).
- `sites/<host>/`: local HTML fixtures and robots.txt served by `FixtureFetcher`. No network is used. One site (fjordmetrics) disallows `/pricing` in robots.txt.
- `results-<model>-<date>.md`: output of `triage eval`.

Expected tiers were written before the first model run and have not been changed to fit any model.

The fictional label sits in an HTML comment, which the extractor strips like any comment, rather than in visible page text. In the first run the label was visible ("Not a real business" in each footer, "(Demo)" in each company name) and the model correctly marked almost everything as not a real business, which measured the label instead of the scoring. That run is kept for the record in the project REPORT.md.
