## Description
<!-- What does this PR do? Which issue does it fix? -->
Fixes # (issue)

## Type of Change
- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Code refactoring
- [ ] Documentation update
- [ ] Performance improvement

## How Has This Been Tested?
- [ ] `python -m pytest` passes
- [ ] `python -m py_compile zulip/adapter.py` passes
- [ ] `python -m py_compile zulip/__init__.py` passes
- [ ] `python -c "import yaml; yaml.safe_load(open('zulip/plugin.yaml'))"` passes
- [ ] Added/updated tests for new code
- [ ] Manual testing against live Zulip server (if applicable)

## Checklist
- [ ] My code follows the style of this project
- [ ] I have performed a self-review of my own code
- [ ] I have added tests that prove my fix/feature works
- [ ] New and existing tests pass locally
- [ ] I have updated documentation (README.md / AGENTS.md / CHANGELOG.md) if applicable
- [ ] I have added env vars to `plugin.yaml` if new configuration is introduced
- [ ] I ran the pre-push hook: `bash .githooks/pre-push` (or it ran automatically)
