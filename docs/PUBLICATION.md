# GitHub publication inventory

A separate publication copy of paper v4 was prepared on 2026-10-26. The original experiment folder was not deleted, moved, or modified.

Included: the current diagnosis core, reused earlier modules, v4 experiment and validation tools, remediation evaluation code, required regression tests, three Node.js examples, public input packets, settings without credentials, and final result tables with their limitations.

Excluded during preparation: the credential-bearing `.env`, virtual environments, bytecode and pytest/Ruff caches, `.cv2t*` and other temporary test directories, intermediate mock and failed-run outputs, original model responses and call records, Docker execution logs, the complete preservation manifest, instructions containing personal paths, working manuscript drafts and implementation handoffs, and tests dedicated to earlier notebooks. The accompanying English paper PDF was subsequently added to the repository root.

The `fixtures` and fake responses in the code are required test data. Removing them would break regression checks and example execution. Modules with earlier version names are retained when imported by current code. The original experiment and its scoring criteria were not changed to improve the appearance of past results.

The latest analysis status is **All scheduled units processed; experiment concluded**, with one API timeout retained as a failed observation. Published results distinguish original responses from limited normalization. COPY detection limitations, the development-stage evaluation scope, and non-independent repetitions are preserved.

The publication folder was initialized as an independent Git repository, so unrelated projects in the parent workspace need not be included. At the preparation step, no remote repository was created and no commit or push was performed; subsequent repository actions belong to the owner. No new repository-wide license was assigned because that choice belongs to the owner.

The public documentation is now available in English. The two Korean runtime prompt files remain byte-identical to the experimental versions; [English reading guides](prompts/README.md) explain them without replacing the model inputs.
