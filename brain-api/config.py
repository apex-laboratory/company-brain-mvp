from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    anthropic_api_key: str
    openai_api_key: str
    semaphore_limit: int = 5
    mcp_port: int = 8001
    api_port: int = 8000

    github_token: str | None = None
    github_repos: str = ""
    github_api_url: str = "https://api.github.com"
    github_per_page: int = 100
    github_max_pages: int = 200
    github_request_timeout: float = 30.0

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()


def configured_github_repos() -> list[str]:
    """Comma-separated GITHUB_REPOS into a clean list of 'owner/repo' strings."""
    return [r.strip() for r in settings.github_repos.split(",") if r.strip()]
