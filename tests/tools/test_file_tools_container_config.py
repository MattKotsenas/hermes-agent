"""Tests for docker container_config key propagation in file_tools."""

from unittest.mock import patch, MagicMock
import tools.file_tools as file_tools


def _make_env_config(**overrides):
    base = {
        "env_type": "docker",
        "docker_image": "test-image:latest",
        "singularity_image": "docker://test",
        "modal_image": "test",
        "daytona_image": "test",
        "cwd": "/workspace",
        "host_cwd": None,
        "timeout": 180,
        "container_cpu": 2,
        "container_memory": 4096,
        "container_disk": 20480,
        "container_persistent": False,
        "docker_volumes": [],
        "docker_mount_cwd_to_workspace": True,
        "docker_forward_env": ["MY_SECRET", "API_KEY"],
    }
    base.update(overrides)
    return base


class TestFileToolsContainerConfig:
    def _run(self, env_config, task_id):
        captured = {}
        mock_env = MagicMock()

        def fake_create_env(**kwargs):
            captured.update(kwargs)
            return mock_env

        with patch("tools.terminal_tool._get_env_config", return_value=env_config),              patch("tools.terminal_tool._task_env_overrides", {}),              patch("tools.terminal_tool._active_environments", {}),              patch("tools.terminal_tool._creation_locks", {}),              patch("tools.terminal_tool._creation_locks_lock", __import__("threading").Lock()),              patch("tools.terminal_tool._create_environment", side_effect=fake_create_env),              patch("tools.terminal_tool._start_cleanup_thread"),              patch("tools.terminal_tool._check_disk_usage_warning"),              patch("tools.file_tools._file_ops_cache", {}),              patch("tools.file_tools._file_ops_lock", __import__("threading").Lock()):
            file_tools._get_file_ops(task_id)

        return captured.get("container_config", {})

    def test_docker_mount_cwd_to_workspace_passed(self):
        """docker_mount_cwd_to_workspace is forwarded to container_config."""
        cc = self._run(_make_env_config(docker_mount_cwd_to_workspace=True), "t1")
        assert cc.get("docker_mount_cwd_to_workspace") is True

    def test_docker_forward_env_passed(self):
        """docker_forward_env is forwarded to container_config."""
        cc = self._run(_make_env_config(docker_forward_env=["MY_SECRET"]), "t2")
        assert cc.get("docker_forward_env") == ["MY_SECRET"]

    def test_docker_mount_cwd_defaults_to_false(self):
        """docker_mount_cwd_to_workspace defaults to False when absent from config."""
        cfg = _make_env_config()
        del cfg["docker_mount_cwd_to_workspace"]
        cc = self._run(cfg, "t3")
        assert cc.get("docker_mount_cwd_to_workspace") is False

    def test_docker_forward_env_defaults_to_empty_list(self):
        """docker_forward_env defaults to [] when absent from config."""
        cfg = _make_env_config()
        del cfg["docker_forward_env"]
        cc = self._run(cfg, "t4")
        assert cc.get("docker_forward_env") == []


# ----------------------------------------------------------------------
# Regression for the gondolin_config plumbing gap discovered 2026-05-30:
# file_tools._get_file_ops never extracted `terminal.gondolin.*` config
# before calling _create_environment, so the gondolin branch of
# _create_environment saw `gondolin_config=None`, the factory's
# _xform_image was invoked with `image=None`, and the agent saw
# "Invalid gondolin image configuration: None" from any write_file /
# patch call that was the FIRST creator of the env (i.e. after the
# cleanup thread had reaped the prior one). The corresponding
# terminal_tool.py path at line ~2214 already handled this correctly;
# file_tools.py was missing the same three-line extraction.
# ----------------------------------------------------------------------

class TestFileToolsGondolinConfig:
    def _run(self, env_config, task_id):
        """Mirror of TestFileToolsContainerConfig._run that captures
        gondolin_config instead of container_config."""
        from unittest.mock import patch, MagicMock
        captured = {}
        mock_env = MagicMock()

        def fake_create_env(**kwargs):
            captured.update(kwargs)
            return mock_env

        with patch("tools.terminal_tool._get_env_config", return_value=env_config), \
             patch("tools.terminal_tool._task_env_overrides", {}), \
             patch("tools.terminal_tool._active_environments", {}), \
             patch("tools.terminal_tool._creation_locks", {}), \
             patch("tools.terminal_tool._creation_locks_lock", __import__("threading").Lock()), \
             patch("tools.terminal_tool._create_environment", side_effect=fake_create_env), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.terminal_tool._check_disk_usage_warning"), \
             patch("tools.file_tools._file_ops_cache", {}), \
             patch("tools.file_tools._file_ops_lock", __import__("threading").Lock()):
            file_tools._get_file_ops(task_id)

        return captured

    def test_gondolin_config_forwarded_to_create_environment(self):
        """The `terminal.gondolin` block must reach _create_environment
        as `gondolin_config=`, mirroring terminal_tool.py's path. Without
        this, the factory's image resolver gets image=None and raises
        "Invalid gondolin image configuration: None" — which the user
        hit on write_file calls after the env was auto-reaped."""
        env_config = _make_env_config(env_type="gondolin")
        env_config["gondolin"] = {
            "image": "nikolaik/python-nodejs:python3.11-nodejs20",
            "stub_vm": True,
        }
        captured = self._run(env_config, "gondolin-test-task")
        gc = captured.get("gondolin_config")
        assert gc is not None, (
            f"_create_environment was called without gondolin_config; "
            f"captured kwargs were: {list(captured.keys())!r}. "
            f"The gondolin branch of _create_environment will see "
            f"gondolin_config=None and the factory's _xform_image will "
            f"call ensure_image_built(None), raising 'Invalid gondolin "
            f"image configuration: None'."
        )
        assert gc.get("image") == "nikolaik/python-nodejs:python3.11-nodejs20"
        assert gc.get("stub_vm") is True

    def test_gondolin_config_passes_empty_dict_when_block_absent(self):
        """If the user has env_type=gondolin but no `terminal.gondolin:`
        block (unusual but legal — gondolin has built-in defaults for
        most knobs), pass {} so the factory uses its own defaults instead
        of None (which would crash _xform_image)."""
        env_config = _make_env_config(env_type="gondolin")
        # No "gondolin" key in env_config.
        captured = self._run(env_config, "gondolin-no-block")
        gc = captured.get("gondolin_config")
        # Must not be None — the gondolin branch of _create_environment
        # treats {} and None differently for the factory call.
        assert gc is not None, (
            "gondolin_config should be {} (or a dict), never None, "
            "when env_type is gondolin"
        )
        assert isinstance(gc, dict)

    def test_non_gondolin_backend_does_not_set_gondolin_config(self):
        """For docker/modal/etc., gondolin_config must be omitted or
        None — passing a stray gondolin block could surface to the wrong
        backend."""
        env_config = _make_env_config(env_type="docker")
        env_config["gondolin"] = {"image": "should-not-leak"}
        captured = self._run(env_config, "docker-task")
        # Either omitted entirely or explicitly None.
        assert captured.get("gondolin_config") in (None,), (
            f"Non-gondolin backend should not receive a gondolin_config; "
            f"got {captured.get('gondolin_config')!r}"
        )
