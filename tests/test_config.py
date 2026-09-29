"""RuntimeConfig 单元测试：默认值、环境变量覆盖、非法值回退与开关字段。"""

import os
from pathlib import Path

import pytest

from harness.config import RuntimeConfig, load_dotenv

_CLEAN_ENV_TARGET_PREFIX = "HARNESS_"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例运行前清掉外部环境可能残留的 LLM_* / HARNESS_* 变量，保证密闭。

    用 monkeypatch.delenv（raising=False）删除，用例结束后自动还原，
    不影响 pytest 进程外的真实环境。
    """
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    for key in list(os.environ):
        if key.upper().startswith(_CLEAN_ENV_TARGET_PREFIX):
            monkeypatch.delenv(key, raising=False)


class TestRuntimeConfig:
    """覆盖 RuntimeConfig 默认值与 from_env 环境变量覆盖行为。"""

    def testDefaultValues(self) -> None:
        """直接构造（不传参）时全部字段取设计定稿的默认值。"""
        config = RuntimeConfig()
        assert config.model == "deepseek-flash"
        assert config.base_url == "https://api.deepseek.com"
        assert config.thinking_enabled is True
        assert config.stream_enabled is True
        assert config.max_rounds == 15
        assert config.compact_rounds == 60
        assert config.compact_tokens == 100_000
        assert config.keep_recent_rounds == 5
        assert config.idle_seconds == 7200
        assert config.scan_interval_seconds == 300
        assert config.llm_timeout_seconds == 60.0
        assert config.llm_max_retries == 2
        assert config.tool_result_max_chars == 2000
        assert config.data_dir == Path("data")

    def testEnvOverrideModel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LLM_MODEL / LLM_BASE_URL 存在时 from_env 覆盖 model 与 base_url。"""
        monkeypatch.setenv("LLM_MODEL", "m1")
        monkeypatch.setenv("LLM_BASE_URL", "http://x")
        config = RuntimeConfig.from_env()
        assert config.model == "m1"
        assert config.base_url == "http://x"

    def testHarnessEnvOverride(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HARNESS_MAX_ROUNDS 覆盖 max_rounds，其余字段保持默认。"""
        monkeypatch.setenv("HARNESS_MAX_ROUNDS", "3")
        config = RuntimeConfig.from_env()
        assert config.max_rounds == 3
        assert config.model == "deepseek-flash"
        assert config.base_url == "https://api.deepseek.com"
        assert config.thinking_enabled is True
        assert config.stream_enabled is True
        assert config.compact_rounds == 60
        assert config.compact_tokens == 100_000
        assert config.keep_recent_rounds == 5
        assert config.idle_seconds == 7200
        assert config.scan_interval_seconds == 300
        assert config.llm_timeout_seconds == 60.0
        assert config.llm_max_retries == 2
        assert config.tool_result_max_chars == 2000

    def testInvalidEnvFallsBack(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HARNESS_MAX_ROUNDS 为非法整数时不抛异常、不产生 None，回退默认 15。"""
        monkeypatch.setenv("HARNESS_MAX_ROUNDS", "abc")
        config = RuntimeConfig.from_env()
        assert config.max_rounds == 15
        assert config.max_rounds is not None

    def testThinkingAndStreamFlags(self) -> None:
        """thinking_enabled / stream_enabled 可独立关闭且不影响其余默认值。"""
        config = RuntimeConfig(thinking_enabled=False, stream_enabled=False)
        assert config.thinking_enabled is False
        assert config.stream_enabled is False
        assert config.model == "deepseek-flash"
        assert config.base_url == "https://api.deepseek.com"
        assert config.max_rounds == 15
        assert config.compact_rounds == 60
        assert config.compact_tokens == 100_000
        assert config.keep_recent_rounds == 5
        assert config.idle_seconds == 7200
        assert config.scan_interval_seconds == 300
        assert config.llm_timeout_seconds == 60.0
        assert config.llm_max_retries == 2
        assert config.tool_result_max_chars == 2000


class TestLoadDotenv:
    """覆盖 load_dotenv 的解析与环境变量加载逻辑。"""

    def testLoadDotenvFileNotFound(self, tmp_path: Path) -> None:
        """文件不存在时返回 False 且不抛异常。"""
        not_exist = tmp_path / ".env.nonexistent"
        assert load_dotenv(not_exist) is False

    def testLoadDotenvBasic(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """基本键值对、注释与空行忽略。"""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# 这是一个注释\n"
            "\n"
            "DEEPSEEK_API_KEY=sk-test-12345\n"
            "HARNESS_MAX_ROUNDS=20\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("HARNESS_MAX_ROUNDS", raising=False)
        loaded = load_dotenv(env_file)
        assert loaded is True
        assert os.environ.get("DEEPSEEK_API_KEY") == "sk-test-12345"
        assert os.environ.get("HARNESS_MAX_ROUNDS") == "20"

    def testLoadDotenvQuotesAndExport(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """带单双引号包裹的值及 export 前缀。"""
        env_file = tmp_path / ".env"
        env_file.write_text(
            'export LLM_MODEL="deepseek-chat"\n'
            "LLM_BASE_URL='https://custom.api.com'\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("LLM_MODEL", raising=False)
        monkeypatch.delenv("LLM_BASE_URL", raising=False)
        assert load_dotenv(env_file) is True
        assert os.environ.get("LLM_MODEL") == "deepseek-chat"
        assert os.environ.get("LLM_BASE_URL") == "https://custom.api.com"

    def testLoadDotenvOverrideBehavior(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认 override=True 时覆盖既有环境变量；override=False 时保留既有环境变量。"""
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_KEY=from_file\n", encoding="utf-8")
        monkeypatch.setenv("TEST_KEY", "from_sys")
        assert load_dotenv(env_file, override=False) is True
        assert os.environ.get("TEST_KEY") == "from_sys"

        assert load_dotenv(env_file) is True
        assert os.environ.get("TEST_KEY") == "from_file"

    def testLoadDotenvEmptyValueSkipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空值行（如 KEY=）自动跳过，不覆盖或置空既有环境变量。"""
        env_file = tmp_path / ".env"
        env_file.write_text("EMPTY_KEY=\n", encoding="utf-8")
        monkeypatch.setenv("EMPTY_KEY", "keep_me")
        assert load_dotenv(env_file) is True
        assert os.environ.get("EMPTY_KEY") == "keep_me"


