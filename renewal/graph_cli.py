"""Read live scoped Notion graph metadata without a local content cache."""
import argparse
import json
import sys
from pathlib import Path

from .graph import GraphStore, TYPES, RELATIONS, STATUSES
from .notion import PrivacyError, RemoteError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--namespace', required=True, choices=['work', 'personal'])
    parser.add_argument('--scope', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('recall', 'inspect'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--node', required=name == 'inspect')
        if name == 'recall':
            cmd.add_argument('--query', default='')
        cmd.add_argument('--budget-chars', type=int, default=4000)
        cmd.add_argument('--hops', type=int, default=2)
        cmd.add_argument('--max-nodes', type=int, default=40)
        cmd.add_argument('--relation')
        cmd.add_argument('--include-recovery', action='store_true')
        cmd.add_argument('--include-superseded', action='store_true')
    for name in ('remember', 'link'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--id', required=True)
        cmd.add_argument('--name', required=True)
        cmd.add_argument('--source', required=True)
        cmd.add_argument('--status', choices=sorted(STATUSES), default='assumed')
        cmd.add_argument('--reviewed', action='store_true', required=True)
        if name == 'remember':
            cmd.add_argument('--type', choices=sorted(TYPES), required=True)
            cmd.add_argument('--summary-file', type=Path, required=True)
            cmd.add_argument('--owner', required=True)
            cmd.add_argument('--verified', help='ISO date of verification, omitted when unknown')
        else:
            cmd.add_argument('--from-node', required=True)
            cmd.add_argument('--to-node', required=True)
            cmd.add_argument('--relation', choices=sorted(RELATIONS), required=True)
    args = parser.parse_args(argv)
    try:
        store = GraphStore(json.loads(args.config.read_text()))
        if args.command == 'remember':
            result = store.remember(args.namespace, args.scope, args.id, name=args.name,
                                    node_type=args.type, summary=args.summary_file.read_text(),
                                    source=args.source, owner=args.owner, status=args.status,
                                    verified=args.verified, reviewed=args.reviewed)
        elif args.command == 'link':
            result = store.link(args.namespace, args.scope, args.id, name=args.name,
                                from_node=args.from_node, to_node=args.to_node, relation=args.relation,
                                source=args.source, status=args.status, reviewed=args.reviewed)
        else:
            result = store.recall(args.namespace, args.scope, query=getattr(args, 'query', ''),
                              node=args.node, hops=args.hops, max_nodes=args.max_nodes,
                              relation=args.relation, budget_chars=args.budget_chars,
                              include_recovery=args.include_recovery,
                              include_superseded=args.include_superseded)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (PrivacyError, RemoteError, ValueError, KeyError, OSError):
        print('Live graph operation failed. Check scope, bounds, authentication and privacy policy. No stale fallback was used. Review any attempted write before retrying.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
