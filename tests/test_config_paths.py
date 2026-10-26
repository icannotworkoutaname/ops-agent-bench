"""配置里的路径：不能写死某个用户的家目录，所以要支持 ~。"""

from pathlib import Path

from bench import config


def test_music_streaming_repo_expands_the_home_directory(monkeypatch):
    raw = config._load_yaml("env.yaml")
    raw["music_streaming_repo"] = "~/music-streaming-analytics"
    monkeypatch.setattr(config, "_load_yaml", lambda name: raw)
    path = config.load_env().music_streaming_repo
    assert path == Path.home() / "music-streaming-analytics"
