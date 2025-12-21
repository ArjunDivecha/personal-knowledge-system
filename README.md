# Knowledge System

A personal knowledge management system that distills AI chat histories (Claude, GPT) into structured, retrievable knowledge entries accessible via an MCP server during Claude conversations.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    LOCAL (M4 Max Mac)                       │
├─────────────────────────────────────────────────────────────┤
│  Dropbox Exports                 Python Distillation        │
│  ├── Claude conversations.json   ├── Parse                  │
│  └── GPT conversations.json      ├── Filter (score >= 3)    │
│                                  ├── Extract (Claude API)   │
│                                  ├── Merge                  │
│                                  ├── Compress               │
│                                  └── Index                  │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    CLOUD STORAGE                            │
├─────────────────────────────────────────────────────────────┤
│  Upstash Redis                   Upstash Vector             │
│  ├── Knowledge entries           └── Entry embeddings       │
│  ├── Project entries                 (1536 dims)            │
│  └── Thin index                                             │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    VERCEL EDGE                              │
├─────────────────────────────────────────────────────────────┤
│  MCP Server (/mcp endpoint)                                 │
│  ├── get_index()     - Returns thin index                   │
│  ├── get_context()   - Returns entry summary                │
│  ├── get_deep()      - Returns full entry                   │
│  └── search()        - Semantic search                      │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    CLAUDE PLATFORMS                         │
├─────────────────────────────────────────────────────────────┤
│  Desktop       iOS App        Web (claude.ai)               │
│  └──────────── Claude Skill ──────────────┘                 │
│                (Routing logic for when to call tools)       │
└─────────────────────────────────────────────────────────────┘
```

## Project Structure

```
knowledge-system/
├── distillation/           # Local Python pipeline
│   ├── main.py             # CLI entry point
│   ├── config.py           # Configuration
│   ├── pipeline/           # Processing stages
│   │   ├── parse.py        # Claude/GPT export parsing
│   │   ├── filter.py       # Value scoring
│   │   ├── extract.py      # LLM extraction
│   │   ├── merge.py        # Entry merging
│   │   ├── compress.py     # Compression/archiving
│   │   └── index.py        # Index generation
│   ├── prompts/            # LLM prompts
│   ├── storage/            # Upstash clients
│   ├── types/              # Data models
│   └── utils/              # Helpers
│
├── mcp-server/             # Vercel TypeScript server
│   ├── api/mcp.ts          # Main API handler
│   ├── src/
│   │   ├── tools/          # MCP tool implementations
│   │   ├── storage/        # Upstash clients
│   │   └── types/          # TypeScript types
│   └── vercel.json         # Deployment config
│
├── skill/                  # Claude Skill
│   ├── SKILL.md            # Routing instructions
│   └── examples/           # Usage examples
│
└── docs/                   # PRDs and guides
```

## Quick Start

### 1. Setup Upstash

1. Create Redis database at [upstash.com](https://upstash.com)
2. Create Vector index (1536 dimensions, cosine similarity)
3. Copy credentials

### 2. Configure Environment

```bash
cd distillation
cp env.example .env
# Edit .env with your credentials
```

Required environment variables:
- `UPSTASH_REDIS_REST_URL`
- `UPSTASH_REDIS_REST_TOKEN`
- `UPSTASH_VECTOR_REST_URL`
- `UPSTASH_VECTOR_REST_TOKEN`
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `MCP_AUTH_TOKEN`

### 3. Install Dependencies

```bash
# Distillation pipeline
cd distillation
pip install -r requirements.txt

# MCP server
cd ../mcp-server
npm install
```

### 4. Test Connection

```bash
cd distillation
python test_connection.py
```

### 5. Run Pipeline

```bash
# Dry run (preview)
python main.py --dry-run

# Full run
python main.py --run

# Limited run (testing)
python main.py --run --limit 5
```

### 6. Deploy MCP Server

```bash
cd mcp-server
vercel env add UPSTASH_REDIS_REST_URL
vercel env add UPSTASH_REDIS_REST_TOKEN
vercel env add UPSTASH_VECTOR_REST_URL
vercel env add UPSTASH_VECTOR_REST_TOKEN
vercel env add OPENAI_API_KEY
vercel env add MCP_AUTH_TOKEN
vercel --prod
```

### 7. Configure Claude

1. Add MCP Connector in Claude settings with your Vercel URL
2. Upload skill ZIP (skill/SKILL.md + examples)
3. Test with "What are my current projects?"

## Pipeline Stages

1. **Parse**: Convert Claude/GPT exports to normalized format
2. **Filter**: Score conversations (keep score >= 3)
3. **Extract**: Use Claude to extract knowledge with evidence
4. **Merge**: Combine with existing entries, handle evolution/contests
5. **Compress**: Archive old entries, generate compressed views
6. **Index**: Update Upstash storage and thin index

## MCP Tools

| Tool | Purpose | When to Use |
|------|---------|-------------|
| `get_index()` | Overview of all knowledge | Start of conversation |
| `get_context(topic)` | Quick summary of topic | Discussing specific topic |
| `get_deep(id)` | Full entry with evidence | Need provenance/evolution |
| `search(query)` | Semantic search | "Have we discussed X?" |

## Key Concepts

- **Provenance**: Every insight links to source message IDs
- **Conservative Merging**: Never silently overwrite; track evolution
- **Contested State**: Preserves both sides when views contradict
- **Thin Index**: ~3000 token summary for fast context injection
- **Compression**: Old entries archived, compressed view kept active

## Troubleshooting

### No conversations parsed
- Check export paths in `config.py`
- Verify JSON files exist in Dropbox folders

### Extraction errors
- Check ANTHROPIC_API_KEY is valid
- Reduce `--limit` if hitting rate limits

### MCP not responding
- Verify Vercel deployment succeeded
- Check environment variables are set
- Test with curl: `curl -X POST https://your-app.vercel.app/mcp -H "Authorization: Bearer $TOKEN" -d '{"tool": "get_index", "arguments": {}}'`

## Version History

- **1.0.0** (December 2024): Initial implementation

