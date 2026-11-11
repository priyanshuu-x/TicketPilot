from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv()


class Settings(BaseSettings):
    primary_model: str = "groq:openai/gpt-oss-120b"
    fallback_model: str = "google_genai:gemini-2.5-flash"
    judge_model: str = "groq:openai/gpt-oss-20b"
    max_retries: int = 2

    support_db_path: str = "support.db"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8001
    mcp_url: str = "http://127.0.0.1:8001/mcp"

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_url: str = "http://127.0.0.1:8000"

    database_url: str = "postgresql://ticketpilot:ticketpilot@localhost:5432/ticketpilot"

    max_steps: int = 10
    max_handoffs: int = 5
    max_messages: int = 6
    refund_approval_limit: float = 2000.0


settings = Settings()
