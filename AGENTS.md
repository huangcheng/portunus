# Portunus — agent instructions

## Commits are gated, mechanically

Every commit (agent or human) runs `ocr review --audience agent` first. Do **not** chain
`ocr review … && git commit`: the chain commits on the command's exit code, not on the
findings — findings have been committed unread that way more than once.

Use the wrapper, which counts findings and refuses to commit unless they are zero or an
explicit reason is recorded in the commit body:

    scripts/gated-commit.sh -m "subject" -m "body"
    scripts/gated-commit.sh -m "subject" --allow "finding N is X because Y"

If `ocr` is missing or broken, say so and stop — do not commit around the gate.
