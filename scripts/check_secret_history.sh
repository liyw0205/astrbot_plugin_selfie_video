#!/bin/sh
set -eu

pattern='sk-[[:alnum:]_-]{32,}'
for commit in $(git rev-list --all); do
    if git grep -I -q -E "$pattern" "$commit" -- .; then
        printf 'Potential API credential found in Git commit %s\n' "$commit" >&2
        exit 1
    fi
done

printf 'No API credential patterns found in Git history.\n'
