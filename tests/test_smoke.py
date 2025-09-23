from pathlib import Path


def test_config_templates_exist():
    assert Path('config.example.yaml').exists(), 'config.example.yaml should be in repo'
    assert Path('.env.example').exists(), '.env.example should be in repo'


def test_import_core_modules():
    # Basic imports should succeed
    import src.bot_app  # noqa: F401
    import src.config_service  # noqa: F401
    import src.message_router  # noqa: F401


def test_prompt_engine_handles_missing_files(tmp_path, monkeypatch):
    # Create a temporary empty files scenario
    from src.prompt_template_engine import PromptTemplateEngine
    from src.persona_service import PersonaService

    # Use non-existent paths to ensure safe fallbacks
    p = PersonaService(path=str(tmp_path / 'no-persona.md'))
    engine = PromptTemplateEngine(system_prompt_path=str(tmp_path / 'no-system.txt'),
                                  context_template_path=str(tmp_path / 'no-context.txt'),
                                  persona_service=p)
    # Should build a system message without raising
    sm = engine.build_system_message()
    assert isinstance(sm, str)

    ctx = engine.render(conversation_window=[], user_input="hi")
    assert isinstance(ctx, str)
