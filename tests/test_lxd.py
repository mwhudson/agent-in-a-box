# Tests for the pure helper functions in aiab.lxd.

from aiab.lxd import container_name_for_dir, dir_slug, is_source_device


# ---------------------------------------------------------------------------
# container_name_for_dir
# ---------------------------------------------------------------------------


def test_name_contains_basename():
    name = container_name_for_dir("/home/user/my-project", "claude")
    assert "my-project" in name


def test_name_prefixed_with_agent():
    name = container_name_for_dir("/home/user/my-project", "claude")
    assert name.startswith("claude-")


def test_name_is_stable():
    a = container_name_for_dir("/home/user/my-project", "claude")
    b = container_name_for_dir("/home/user/my-project", "claude")
    assert a == b


def test_same_basename_different_dirs_differ():
    a = container_name_for_dir("/home/alice/project", "claude")
    b = container_name_for_dir("/home/bob/project", "claude")
    assert a != b


def test_name_only_lowercase_alnum_and_hyphens():
    name = container_name_for_dir("/home/user/My Project!", "claude")
    # LXD container names must be lowercase alphanumeric + hyphens.
    import re

    assert re.fullmatch(r"[a-z0-9-]+", name)


def test_name_no_leading_or_trailing_hyphen_in_basename_part():
    # Paths with leading/trailing special chars in basename shouldn't produce
    # double-hyphens at the basename boundary.
    name = container_name_for_dir("/home/user/---weird---", "claude")
    assert "--" not in name


def test_name_length_within_lxd_limit():
    long_path = "/home/user/" + "a" * 200
    name = container_name_for_dir(long_path, "claude")
    # LXD container names are capped at 63 chars.
    assert len(name) <= 63


# ---------------------------------------------------------------------------
# dir_slug
# ---------------------------------------------------------------------------


def test_container_name_is_prefixed_slug():
    # The per-directory state dirs (aiab.state) are named with the bare slug,
    # so this keeps them correlatable with the container names.
    path = "/home/user/my-project"
    assert container_name_for_dir(path, "claude") == "claude-" + dir_slug(path)


def test_dir_slug_sanitised():
    import re

    assert re.fullmatch(r"[a-z0-9-]+", dir_slug("/home/user/My Project!"))


# ---------------------------------------------------------------------------
# is_source_device
# ---------------------------------------------------------------------------


def test_source_device_recognised():
    # container_name_for_dir embeds the last 6 chars of the path hash as the
    # container name suffix; is_source_device does a prefix match on the
    # dir-<hash> device name.
    import hashlib

    path = "/home/user/my-project"
    container_name = container_name_for_dir(path, "claude")
    path_hash = container_name.rsplit("-", 1)[-1]  # 6-char suffix
    device_name = f"dir-{path_hash}abcd"  # longer hash, as _device_name produces
    assert is_source_device(device_name, container_name)


def test_non_source_device_not_recognised():
    container_name = container_name_for_dir("/home/user/project", "claude")
    # A device with an unrelated hash should not match.
    assert not is_source_device("dir-zzzzzzzz", container_name)


def test_extra_mount_not_recognised():
    # A second directory mounted into the same container has a different hash.
    import hashlib

    container_name = container_name_for_dir("/home/user/project", "claude")
    other_hash = hashlib.md5(b"/home/user/other").hexdigest()[:8]
    assert not is_source_device(f"dir-{other_hash}", container_name)
