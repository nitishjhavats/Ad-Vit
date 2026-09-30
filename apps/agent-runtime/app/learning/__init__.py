"""The closed loop: a prediction is recorded, then measured, then learned from.

`decisions` already carried `expected_effect_json` and `horizon_days`, written
before anything executed, and `outcomes` already had a placeholder row queued at
execution. Nothing ever measured one. So the product's central claim - that it
learns from what its own proposals actually did - had a schema and no code.
"""
