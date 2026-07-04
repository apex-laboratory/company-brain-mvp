"""Prompt text for the pipeline's LLM stages, as module constants.

One module per stage. Keeping prompts as code (reviewed, diffed, versioned)
makes the eval harness (``evals/``) the regression suite for every prompt
change — edit a prompt, re-run the evals, compare the metrics.
"""
