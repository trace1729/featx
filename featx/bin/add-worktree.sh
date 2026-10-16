#!/usr/bin/env bash
# Create a git worktree from a base branch and share the superproject's
# existing submodule checkouts via symlinks (avoids re-cloning submodules).
#
# Usage:
#   add-worktree-with-shared-submodules.sh <repo> <base-branch> <worktree-name> [--path <path>]
#
# Output:
#   Prints the absolute path of the created worktree as the last line.
#
# Behavior:
#   - Creates worktree at <repo-parent>/<worktree-name> (or --path).
#   - For every submodule declared in <worktree>/.gitmodules, replaces the
#     empty checkout dir with a symlink to <repo>/<submodule-path>, so the new
#     worktree shares the submodule working trees (incl. uncommitted changes).
#   - Nested submodules (e.g. rocket-chip/hardfloat) are covered automatically
#     because they live inside the shared parent submodule checkout.

set -euo pipefail

die() {
    echo "error: $*" >&2
    exit 1
}

usage() {
    sed -n '2,12p' "$0" >&2
    exit 1
}

if [[ $# -lt 3 ]]; then
    usage
fi

REPO=$(cd "$1" && pwd)
BASE_BRANCH=$2
WT_NAME=$3
shift 3

CUSTOM_PATH=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --path)
            [[ $# -ge 2 ]] || usage
            CUSTOM_PATH=$2
            shift 2
            ;;
        *)
            usage
            ;;
    esac
done

# ---- validation -------------------------------------------------------------
[[ -d "$REPO/.git" || -f "$REPO/.git" ]] || die "not a git repo: $REPO"
git -C "$REPO" rev-parse --verify --quiet "refs/heads/$BASE_BRANCH" >/dev/null \
    || git -C "$REPO" rev-parse --verify --quiet "$BASE_BRANCH" >/dev/null \
    || die "base branch not found: $BASE_BRANCH"
git -C "$REPO" rev-parse --verify --quiet "refs/heads/$WT_NAME" >/dev/null \
    && die "branch already exists: $WT_NAME"

if [[ -n "$CUSTOM_PATH" ]]; then
    WT_PATH=$(cd "$(dirname "$CUSTOM_PATH")" && pwd)/$(basename "$CUSTOM_PATH")
else
    WT_PATH="$(dirname "$REPO")/$WT_NAME"
fi
[[ -e "$WT_PATH" ]] && die "target path already exists: $WT_PATH"

# ---- create worktree --------------------------------------------------------
git -C "$REPO" worktree add -b "$WT_NAME" "$WT_PATH" "$BASE_BRANCH"

# ---- symlink shared submodules ----------------------------------------------
MODULES_FILE="$WT_PATH/.gitmodules"
if [[ -f "$MODULES_FILE" ]]; then
    while IFS= read -r sub_path; do
        [[ -n "$sub_path" ]] || continue
        src="$REPO/$sub_path"
        dst="$WT_PATH/$sub_path"

        if [[ ! -e "$src/.git" ]]; then
            echo "warning: source submodule not checked out, skipping: $sub_path" >&2
            continue
        fi

        if [[ -L "$dst" ]]; then
            rm -f "$dst"
        elif [[ -d "$dst" ]]; then
            # empty placeholder dir left by `git worktree add`
            if [[ -z "$(ls -A "$dst" 2>/dev/null)" ]]; then
                rmdir "$dst"
            else
                echo "warning: destination not empty, skipping symlink: $dst" >&2
                continue
            fi
        fi

        mkdir -p "$(dirname "$dst")"
        ln -s "$src" "$dst"
        echo "linked $dst -> $src"
    done < <(git -C "$WT_PATH" config -f "$MODULES_FILE" --get-regexp '^submodule\..*\.path$' | cut -d' ' -f2-)
fi

echo "$WT_PATH"
