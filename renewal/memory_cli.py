"""Explicit scoped memory entry point for either harness. No local read cache."""
import argparse
import json
from pathlib import Path

from .lifecycle import MemoryService
from .notion import NotionStore, NtnTransport


def service(path):
    config = json.loads(Path(path).read_text())['notion']
    return MemoryService(NotionStore(config, NtnTransport(config)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--namespace', required=True, choices=['work', 'personal'])
    parser.add_argument('--scope', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    recall = commands.add_parser('recall')
    recall.add_argument('--query', required=True)
    recall.add_argument('--budget-chars', type=int, default=4000)
    for name in ('remember', 'correct', 'forget', 'recover', 'get'):
        command = commands.add_parser(name)
        command.add_argument('--key', required=True)
        if name in ('remember', 'correct'):
            command.add_argument('--body-file', required=True, type=Path)
            command.add_argument('--provenance', required=True, action='append')
        if name == 'remember':
            command.add_argument('--kind', default='project', choices=['project', 'preference', 'evidence', 'lesson'])
            command.add_argument('--theme', default='')
        if name == 'forget':
            command.add_argument('--evidence', action='append', required=True)
            command.add_argument('--confidence', type=float, required=True)
            command.add_argument('--confirmed', action='store_true')
        if name == 'get':
            command.add_argument('--recovery', action='store_true')
    args = parser.parse_args(argv)
    memory = service(args.config)
    route = (args.namespace, args.scope)
    if args.command == 'recall':
        result = memory.recall(*route, args.query, budget_chars=args.budget_chars)
    elif args.command == 'remember':
        result = memory.remember(*route, args.key, args.body_file.read_text(), kind=args.kind,
                                 provenance=args.provenance, theme=args.theme)
    elif args.command == 'correct':
        result = memory.correct(*route, args.key, args.body_file.read_text(), args.provenance)
    elif args.command == 'forget':
        result = memory.forget(*route, args.key, evidence=args.evidence,
                               confidence=args.confidence, confirmed=args.confirmed)
    elif args.command == 'recover':
        result = memory.recover(*route, args.key)
    else:
        result = memory.require(*route, args.key, recovery=args.recovery)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
