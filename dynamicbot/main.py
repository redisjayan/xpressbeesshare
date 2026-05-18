"""CLI entrypoint: run from project root (`python main.py \"your question\"`)."""

from __future__ import annotations

import argparse
import json
import sys

from redisvl_dw.pipeline import answer_question_sync
from redisvl_dw.settings import load_settings
import os
import config

os.environ['OPENAI_API_KEY'] = config.OPENAI_API_KEY
os.environ['REDIS_URL'] = config.REDIS_URL
os.environ['DATABASE_URL'] = config.DATABASE_URL

def main() -> None:
    parser = argparse.ArgumentParser(
        description="RedisVL semantic cache + LLM text-to-SQL + PostgreSQL (MCP or direct).",
    )
    parser.add_argument(
        "question",
        nargs="?",
        default="Total shipment weight in kg for customer Acme Retail EU in January 2026.",
        help="Natural language question over the logistics warehouse.",
    )
    args = parser.parse_args()

    try:
        settings = load_settings()
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    result = answer_question_sync(args.question, settings)
    print(json.dumps(result, indent=2, default=str))

def maindirect(question) :
    
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    result = answer_question_sync(question, settings)
    return result

if __name__ == "__main__":
    main()


#instructions: 

#before running the code, start the MCP server:
#uvx mcp-redis-server --connection-url redis://localhost:6379

#in case running local postgresql, use the following docker command to initiate pg container:
#docker run --name my-postgres -e POSTGRES_PASSWORD=MyPassword -p 5432:5432 -d postgres

#Run the cose with query:
# python main.py "How many shipments were booked in January 2026?"
