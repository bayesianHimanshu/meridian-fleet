# Third-party material

These are the data, fonts and media this repository contains or uses, and those the
published demo page carries. For each, the licence and where it was confirmed. NOTICE holds
the summary. The Apache License 2.0 (LICENSE) covers this project's own code and
documentation only.

## Data

| Data | In the repository? | Licence or terms | Confirmed |
|---|---|---|---|
| CDISC SDTM/ADaM Pilot Project, CDISCPILOT01 (MRD-201) | No. `meridian data fetch` downloads it (config/default.toml [pilot] base_url). Values derived from it appear in docs, tests and the demo. | CDISC's terms of use: attribution required when distributing; no fee; not to mislead as to origin. There is no licence file. | The README of github.com/cdisc-org/sdtm-adam-pilot-project ("Disclaimer and liability", "Terms of use"), read 20 Nov 2026; `gh api` shows no licence. The owner accepted the use with attribution (see below). |
| PHUSE PointCross SEND study, PC201708 (MRD-101-TOX-01) | No. `meridian preclinical fetch` downloads it from phuse-org/phuse-scripts at commit 398a6d3. | MIT License | `gh api repos/phuse-org/phuse-scripts` (spdx MIT); LICENSE.md of the repository; docs/preclinical-data.md |
| openFDA FAERS extracts | No. The signal scout fetches them at scan time into var/ (git-ignored). Counts derived from them appear in docs and the demo. | Public domain, CC0 1.0. Do not imply FDA endorsement. | open.fda.gov/license, read 20 Nov 2026 |
| US label of xanomeline and trospium chloride (openFDA drug/label) | No (fetched with the scan). Short quotations appear in docs/signal-scout.md, each cited with its openFDA set id (8f0e73bf-6025-44f6-ab64-0983322de0df), version 6, effective 2026-01-16. | openFDA content is CC0; the label text is the sponsor's and FDA's. Quotations are kept short and cited, and no endorsement by the FDA or the manufacturer is implied. | open.fda.gov/license; the owner's decision (below) |
| MedDRA terms | Individual terms only, as they appear in public FAERS and CDISC pilot data. They are in data/term_pairs/dev.jsonl, test fixtures and docs. No codes, hierarchy or distribution files. | MedDRA is ICH's terminology; the trademark is ICH's. Only individual terms from public data are included, not the dictionary. | A full-history scan for 8-digit 10xxxxxx codes, code columns (AEPTCD, AELLTCD, ...) and MedDRA file names found none (the only 8-digit 10xxxxxx strings are package sizes in uv.lock), 20 Nov 2026 |
| data/inventory/MRD-101/inventory.csv, data/term_pairs/dev.jsonl, data/narrator_eval/labelled.jsonl, tests/fixtures/* | Yes | This project's own, written for it (Apache-2.0) | Authored in this repository |

The owner's decisions (20 Nov 2026):
- **CDISC.** Accept, with the attribution in NOTICE, the README, this file, the demo page's
  footer and the video's closing card. Add a carve-out saying the Apache License does not
  cover the CDISC data or values derived from it. A courtesy note to CDISC is drafted in
  docs/legal/cdisc-note.md, for the owner to send.
- **MedDRA.** The NOTICE statement above, after the scan found only term text.
- **Label text.** Short, cited quotations. "Xanomeline and trospium chloride" in running
  text, and the brand name only in citations. The openFDA notice is in NOTICE and on the
  Safety signals page.

  The signal scout's own configured context (config/default.toml,
  [agents.safety_signal_scout]) and its recorded findings still use the brand name. They are
  the agent's output, and this step does not change agent behaviour.

## Fonts, media and code

| Item | In the repository? | Licence | Confirmed |
|---|---|---|---|
| IBM Plex Sans, Sans Condensed and Mono (Latin-1 subsets, woff2) | Yes, web/public/fonts. Served by the Command Center; they appear rendered in the demo video. | SIL Open Font License 1.1 | web/public/fonts/LICENSE-IBM-Plex.txt |
| Narration voice: Piper en_US-libritts-high (speaker 0) | No. The voice files live in var/demo/voice; only the narration audio is published, inside demo.mp4. | Dataset LibriTTS, CC BY 4.0. Credit: Zen, H., Dang, V., Clark, R., Zhang, Y., Weiss, R. J., Jia, Y., Chen, Z. and Wu, Y. (2019). LibriTTS: A Corpus Derived from LibriSpeech for Text-to-Speech. Interspeech 2019. | The voice's MODEL_CARD (huggingface.co/rhasspy/piper-voices, en/en_US/libritts/high), sha256 in docs/demo/README.md |
| Piper 1.8.0 (the TTS engine) | No. It is installed separately as a uv tool and run as a separate program. | GPL-3.0-or-later | PyPI metadata of piper-tts 1.8.0 |
| docs/demo/index.html | Yes: this project's own HTML, CSS and JavaScript, with the index and transcript as data. No vendored fonts, scripts or styles (system fonts only), and no external request. | Apache-2.0 (this project) | Built by web/demo/build-page.mjs; a test checks it makes no external request |
| The demo video (demo.mp4) | No: a release asset. Frames of the Command Center (IBM Plex rendered), card pages, and the narration. | This project's, with the CDISC, openFDA and voice credits on its closing card | docs/demo/README.md |
| Python and npm dependencies | No: uv.lock and web/package-lock.json pin them, and uv and npm install them. Nothing is vendored. | Their own licences | uv.lock, web/package-lock.json |
| Apache License 2.0 text (LICENSE) | Yes | Apache License 2.0 | Downloaded from apache.org (sha256 cfc7749b...3d30). The bytes are identical to the copies shipped in sniffio 1.3.1, watchdog 6.0.0 and s3transfer wheels in the local uv cache. |
