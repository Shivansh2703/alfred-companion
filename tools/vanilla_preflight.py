#!/usr/bin/env python3
"""Validate the reviewed Vanilla companion artifact before starting Alfred."""
import argparse, hashlib, json, os, sys

REVIEW_STATUS = "reviewed_offline_independent_approved"

def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def validate(root, lock_path):
    root = os.path.abspath(root)
    with open(lock_path, encoding="utf-8") as stream:
        lock = json.load(stream)
    if not isinstance(lock, dict) or lock.get("schema_version") != 1:
        raise ValueError("invalid Vanilla lock schema")
    if lock.get("preset") != "vanilla" or lock.get("profile") != "vanilla":
        raise ValueError("Vanilla lock must select preset/profile vanilla")
    profile_path = os.path.join(root, ".cache", "profile")
    try:
        with open(profile_path, encoding="utf-8") as stream:
            profile = stream.read()
    except OSError as exc:
        raise ValueError(f"missing Vanilla profile override: {profile_path}") from exc
    if profile != "vanilla":
        raise ValueError(".cache/profile must contain exactly 'vanilla' (no newline or mod profile)")
    rules_rel = lock.get("rules_path", "data/rules.json")
    if os.path.isabs(rules_rel) or os.path.normpath(rules_rel) != "data/rules.json":
        raise ValueError("Vanilla lock must bind data/rules.json")
    rules_path = os.path.join(root, rules_rel)
    try:
        with open(rules_path, encoding="utf-8") as stream:
            rules = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read locked Vanilla rulebook: {rules_path}") from exc
    if not isinstance(rules, dict):
        raise ValueError("locked Vanilla rulebook must be a JSON object")
    actual_sha = _sha256(rules_path)
    if actual_sha != lock.get("rules_sha256"):
        raise ValueError("data/rules.json hash does not match the Vanilla lock")
    if rules.get("profile") != "vanilla":
        raise ValueError("data/rules.json embedded profile is not vanilla")
    build_id = rules.get("game_build_id")
    if not build_id or build_id != lock.get("game_build_id"):
        raise ValueError("data/rules.json build id does not match the Vanilla lock")
    labels = lock.get("labels")
    if not isinstance(labels, dict) or labels.get("review_status") != REVIEW_STATUS:
        raise ValueError("Vanilla labels are missing or not independently reviewed")
    labels_rel = labels.get("path", "data/purpose_labels_vanilla.json")
    if os.path.isabs(labels_rel) or os.path.normpath(labels_rel) != "data/purpose_labels_vanilla.json":
        raise ValueError("Vanilla labels path is outside the companion")
    labels_path = os.path.join(root, labels_rel)
    try:
        with open(labels_path, encoding="utf-8") as stream:
            registry = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ValueError("locked Vanilla labels are missing or malformed") from exc
    if not isinstance(registry, dict):
        raise ValueError("locked Vanilla labels must be a JSON object")
    if _sha256(labels_path) != labels.get("sha256"):
        raise ValueError("Vanilla labels hash does not match the lock")
    coverage = registry.get("coverage")
    if registry.get("rulebook_sha256") != actual_sha or not isinstance(coverage, dict):
        raise ValueError("Vanilla labels are not bound to this rulebook")
    if coverage.get("review_status") != REVIEW_STATUS:
        raise ValueError("Vanilla labels coverage is not independently reviewed")
    if labels.get("rulebook_sha256") != actual_sha:
        raise ValueError("Vanilla labels lock binding does not match the rulebook")
    return f"vanilla preflight ok: rules sha256={actual_sha} build_id={build_id}"

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--lock", required=True)
    args = parser.parse_args(argv)
    try:
        print(validate(args.root, args.lock))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"REFUSING Vanilla start: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
