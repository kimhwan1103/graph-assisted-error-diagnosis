# English reading guides for the original prompts

The experiments used Korean runtime prompts and required Korean natural-language explanations. Translating the active prompts would change the model inputs, even if the intended instructions were similar. The original files are therefore retained byte for byte.

| Runtime prompt | English reading guide | Purpose |
| --- | --- | --- |
| [diagnosis_prompt.md](../../src/ai_error_check_agent/agent/diagnosis_prompt.md) | [Diagnosis prompt](diagnosis-prompt.en.md) | Structured diagnostic output, evidence references, and conditional remediation |
| [interactive_prompt.md](../../src/ai_error_check_agent/agent/interactive_prompt.md) | [Interactive prompt](interactive-prompt.en.md) | Interactive diagnosis guidance and operational boundaries |

The English files in this directory are documentation only. The runtime does not load them, and they were not used to produce the reported experimental results. Their references to Korean output are retained because that was part of the original protocol.

These guides cover the two standalone Markdown prompt files. Additional experimental prompt text and contract appendices are embedded in the Python source; those runtime strings also remain unchanged. English documentation does not imply that model inputs, output-language requirements, stored evidence, or raw responses have been translated.
