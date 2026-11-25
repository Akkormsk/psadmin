"""Record reviewed PSADMIN evidence in a matching GitHub Issue."""
import argparse
import json
import os
import subprocess


def gh(*args):
    return json.loads(subprocess.check_output(['gh', *args], text=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--kind', choices=['error', 'fix', 'verification', 'limitation'], required=True)
    parser.add_argument('--key', required=True, help='Stable domain and scenario, e.g. documents/reading-limit')
    parser.add_argument('--title', required=True)
    parser.add_argument('--scenario', required=True)
    parser.add_argument('--result', required=True)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--evidence-url', required=True)
    parser.add_argument('--scope', choices=['CODE_ONLY', 'TEST_VERIFIED', 'PROD_VERIFIED'], required=True)
    parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY'))
    args = parser.parse_args()
    if not args.repo or len(args.sha) != 40 or any(c not in '0123456789abcdef' for c in args.sha):
        parser.error('Repository and full commit SHA are required')
    if not args.evidence_url.startswith(('https://github.com/' + args.repo + '/',)):
        parser.error('Evidence must link to this repository')
    marker = f'<!-- psadmin-memory:{args.key} -->'
    if not all(c.isalnum() or c in '/._-' for c in args.key):
        parser.error('Invalid stable key')
    issues = gh('issue', 'list', '--repo', args.repo, '--state', 'all', '--limit', '1000',
                '--json', 'number,body')
    matches = [issue for issue in issues if marker in (issue['body'] or '')]
    if len(matches) > 1:
        parser.error('Multiple Issues share the same key; resolve manually')
    entry = (f'### {args.kind}: {args.scope}\n'
             f'Scenario: {args.scenario}\n\nResult: {args.result}\n\n'
             f'Code: `{args.sha}`\nEvidence: {args.evidence_url}\n')
    if matches:
        subprocess.run(['gh', 'issue', 'comment', str(matches[0]['number']), '--repo', args.repo,
                        '--body', entry], check=True)
    else:
        subprocess.run(['gh', 'issue', 'create', '--repo', args.repo, '--title', args.title,
                        '--body', marker + '\n\n' + entry], check=True)


if __name__ == '__main__':
    main()
