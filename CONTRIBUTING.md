# Contributing

Thanks for wanting to help! Bug reports, docs fixes, guide wording and new
features are all welcome. If you're planning something big, open an issue
first so we can agree on the approach before you put time in.

## Getting set up

```bash
git clone https://github.com/<you>/support-bot.git && cd support-bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt ruff pytest
```

You don't need a Slack workspace to run the tests. `tests/conftest.py`
sets dummy tokens and stubs Slack's auth check, and the flow tests use a
fake Slack client. To try a change for real, make a test app with
[`slack-manifest.yml`](slack-manifest.yml) in a workspace you own (a free
workspace is fine), and run `python app.py` with its tokens in `.env`.

## Before you open a PR

```bash
pytest -q          # all tests must pass
ruff check .       # lint
ruff format .      # format (line length 120)
```

CI runs the same three on every PR.

- **Add or update tests** for behaviour you change. `tests/test_flows.py`
  drives the real handlers with a fake client, so it's usually the place
  for anything that posts to Slack. Pure logic goes in
  `tests/test_new_features.py`.
- **Update the docs** if users, helpers or admins will notice the change:
  [`docs/how-it-works.md`](docs/how-it-works.md), the in-Slack guides in
  [`guides.py`](guides.py), and the help text in `app.py`.
- **Match the surrounding code:** comments that explain *why*, small
  functions, no new dependencies unless they're really needed.
- **Keep PRs focused.** One change per PR is much easier to review.

## How the code is laid out

| File | |
|---|---|
| `app.py` | Slack event and action handlers: the relay, tickets, `?` commands, `/support` |
| `home.py` | Block Kit for the Home tab and every modal (pure functions, no Slack calls) |
| `guides.py` | The text of the helper training, admin handbook and "how to get help" |
| `db.py` | SQLite: tickets, replies, settings, scheduled messages. Migrations run in `init_db()` |
| `config.py` | Settings: saved in Slack → env var → default |
| `helpers.py`, `macros.py` | The roster and saved replies (JSON files) |
| `scheduling.py` | `?@user` delays and `{placeholder}` rendering |
| `attachments.py` | Copying files between conversations |

**Schema changes** must keep working on existing databases: add columns
with an `ALTER TABLE` guarded by a `PRAGMA table_info` check in
`init_db()`, like the others there.

**Button and callback IDs** on messages already posted in Slack keep
arriving after an upgrade. Don't rename or remove an `action_id` without
keeping a handler for the old one.

## Using AI tools

Much of this project was written with AI coding assistants (see
[AI transparency](README.md#ai-transparency)), and you're welcome to use
them too, on these conditions:

- **You're the author.** Read and understand every line you submit, and be
  ready to explain and change it in review.
- **Say so.** Mention it in the PR description, or add a
  `Co-Authored-By:` trailer to the commit.
- **Test it for real** when it touches Slack behaviour. Passing tests
  aren't enough when the fake client can't catch something.
- No AI-generated issues or review comments posted without a human
  checking them first.

## Reporting bugs and asking for features

Use the issue templates. For bugs, include what you did, what you
expected, what happened, and the relevant log lines (with tokens removed).
**Security problems go through [SECURITY.md](SECURITY.md), not public
issues.**

## Code of conduct

Everyone taking part is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

By contributing, you agree your contributions are licensed under the
project's [MIT License](LICENSE).
