---
name: lab-text-metrics
description: Test explicitly requested reusable text-metrics artifact creation and reuse.
---

The default tool name is `lab_text_metrics_v1`; use another simple lowercase
underscore name only if the user explicitly chooses it for a separate rerun.
This is an explicit reusable named-tool workflow test. Bash or Python can already
compute text metrics; that fact does not itself justify automatic tool creation.

Discover the exact tool name with list_available_tools. Discovery errors are not
proof of absence. If installed, request access if needed and reuse it; never
overwrite or recreate it. If unavailable, create only when the user explicitly
requests the reusable artifact. Discover and request `tool_authoring` if needed,
subject to approval; stop on denial. Use tool_authoring to create under the
configured artifact root (`~/.<harness-name>/tool-artifacts/<name>/`), never in
the project or a hard-coded home. Creation, execution, and saving profile access
are separate permission decisions. Declining persistence does not remove files
or disable the tool within the current delegation. Do not install dependencies.

Define input_schema as an object requiring `text` of type string, with
additionalProperties false. Implement a Python 3 standard-library command using
an installed runtime (verify availability with approved access if uncertain).
Read one JSON object from stdin and write one JSON object to stdout with only
integer `line_count` and `word_count` fields. Define line_count as 0 for empty
text, otherwise the number of LF characters plus 1 unless the text ends in LF.
Only LF separates lines; CR is an ordinary character for this count. Define
word_count as len(text.split()) using Python Unicode whitespace semantics.
Reject malformed JSON, extra/missing keys, or non-string text with a nonzero
exit status and a fixed safe stderr diagnostic, without echoing input. The
manifest command should use the installed Python executable and
`{artifact_dir}/main.py`. Existing artifacts must never be overwritten.

After creation or acquisition, execute the user's input in this same delegation
and report JSON plus artifact location when created. For `{"text":"alpha beta\ngamma\n"}`
expect `{"line_count":2,"word_count":3}`. For `{"text":""}` expect both 0;
for `{"text":"\n"}` expect line_count 1 and word_count 0; for
`{"text":"alpha\n\n"}` expect line_count 2 and word_count 1. Do not claim
validation that was not executed. Runtime or dependency failures call for
diagnosis, not replacement artifact creation or unapproved installation.
