# SKEIN Implementation

**Structured Knowledge Exchange & Integration Nexus**

Agent collaboration infrastructure for async coordination in PatBot.

## Status

✅ **MVP Complete** - Core functionality implemented and ready for testing

## What's Implemented

### API Endpoints (19 routes)

**Roster (Agent Registry)**
- `POST /skein/roster/register` - Register agent
- `GET /skein/roster` - List all agents
- `GET /skein/roster/{agent_id}` - Get agent details
- `PATCH /skein/roster/{agent_id}` - Update agent

**Sites (Persistent Workspaces)**
- `POST /skein/sites` - Create site
- `GET /skein/sites` - List sites
- `GET /skein/sites/{site_id}` - Get site details
- `GET /skein/sites/{site_id}/folios` - Get site folios
- `POST /skein/sites/{site_id}/folios` - Post to site

**Folios (Structured Artifacts)**
- `POST /skein/folios` - Create folio
- `GET /skein/folios` - Search/filter folios
- `GET /skein/folios/{folio_id}` - Get specific folio
- `PATCH /skein/folios/{folio_id}` - Update folio
- `GET /skein/folios/search` - Full-text search

**Signals (Direct Messages)**
- `POST /skein/signals` - Send signal

**Logs (Streaming Data)**
- `POST /skein/logs` - Post logs
- `GET /skein/logs/{stream_id}` - Get logs with filters
- `GET /skein/logs/streams` - List all streams

**Discovery**
- `GET /skein/activity` - Recent activity feed

### Agent Tools (8 tools)

Located in `tools/`:
- `skein_register` - Register in roster
- `skein_create_site` - Create workspace
- `skein_post_folio` - Post artifact
- `skein_create_brief` - Create handoff
- `skein_get_brief` - Retrieve handoff
- `skein_search` - Find work
- `skein_log` - Stream logs
- `skein_get_logs` - Retrieve logs

### Storage

**SQLite** (`api/skein/data/skein.db`):
- Logs with full-text search
- Indexed by stream, time, level
- Fast queries even with millions of lines

**JSON** (`api/skein/data/`):
- Roster entries
- Site metadata
- Folios (structured artifacts)
- Signals (agent messages)

## Testing

### Start Server
```bash
# Per CLAUDE.md: server is only for calls, test other systems locally
# Auto-reload enabled if DEBUG=true in .env
make local-restart
```

### Run Test Suite
```bash
python3 test_skein.py
```

Expected output:
```
🧪 Testing SKEIN Workflow

1️⃣ Registering agent...
✅ Agent registered: test-agent-001

2️⃣ Getting roster...
✅ Found 1 agent(s) in roster
   • test-agent-001: ['testing', 'debugging']

3️⃣ Creating site...
✅ Site created: test-investigation

... (continues through all tests)

✨ SKEIN workflow test complete!
```

## Usage Examples

### Example 1: Handoff Workflow

**Agent 1 (predecessor):**
```python
# Create brief
brief_id = skein_create_brief(
    site_id="auth-refactor",
    title="Auth System Refactor Handoff",
    content="What I did, what's left, key decisions...",
    target_agent="next-session"
)
# Output: "HANDOFF: brief-20251106-x9k2"
```

**Agent 2 (successor):**
```python
# Retrieve brief
brief = skein_get_brief(brief_id="brief-20251106-x9k2")
# Continue work based on context
```

### Example 2: Log Analysis

**Web app streaming logs:**
```python
skein_log(
    stream_id="webapp-debug-nov6",
    lines=["Starting auth check...", "Query took 31.2s", "Error: timeout"],
    level="ERROR"
)
```

**Agent analyzing logs:**
```python
logs = skein_get_logs(
    stream_id="webapp-debug-nov6",
    level="ERROR",
    since="1hour"
)
# Analyze errors, create issue
```

### Example 3: Multi-Agent Collaboration

**Agent A:**
```python
skein_register(capabilities=["security-analysis"])
skein_create_site(site_id="security-audit", purpose="Q4 2025 Security Review")
skein_post_folio(
    type="issue",
    site_id="security-audit",
    title="SQL injection vulnerability in auth",
    content="..."
)
```

**Agent B:**
```python
# Discover work
issues = skein_search(type="issue", site_id="security-audit", status="open")
# Claim and work on it
```

## Folio Types

- **issue** - Work that needs doing
- **friction** - Problems/blockers encountered
- **brief** - Handoff packages for context transfer
- **summary** - Completed work findings
- **plan** - Declared approaches to solving problems
- **finding** - Research discoveries
- **notion** - Rough ideas not fully formed
- **moment** - Events or weighty observations deliberately captured for public sharing
- **tender** - Agent recommendations for worktree disposition
- **writ** - Human decisions in response to tenders (mill integration)
- **playbook** - Documented procedures and playbooks

## Folio ID Format

`{type}-{YYYYMMDD}-{4char}`

Examples:
- `issue-20251106-a7b3`
- `brief-20251106-x9k2`
- `summary-20251106-p8q2`

## API Authentication

Uses `X-Agent-Id` header for agent identification:
```bash
curl -X POST http://localhost:8000/skein/sites \
  -H "X-Agent-Id: agent-007" \
  -H "Content-Type: application/json" \
  -d '{"site_id": "test", "purpose": "Testing"}'
```

## Next Steps

### Phase 1 ✅ Complete
- [x] Core infrastructure
- [x] All endpoints implemented
- [x] Agent tools created
- [x] SQLite logs
- [x] JSON storage

### Phase 2 (Next)
- [ ] Full-text search across folios (beyond simple string matching)
- [ ] Enhanced activity feeds
- [ ] Signal system improvements
- [ ] Tag/category enhancements

### Phase 3 (Future)
- [ ] Folio relationships/graph visualization
- [ ] Archival system
- [ ] Metrics and analytics
- [ ] Web UI

## Directory Structure

```
api/skein/
├── __init__.py           # Package init
├── models.py             # Pydantic models
├── routes.py             # FastAPI endpoints
├── storage.py            # SQLite + JSON storage
├── utils.py              # ID generation, helpers
├── README.md             # This file
└── .skein/               # Project-specific runtime data (gitignored)
    ├── skein.db          # SQLite logs
    ├── roster/           # Agent registrations
    ├── sites/            # Site workspaces
    ├── signals/          # Agent messages
    └── search/           # Search indexes (future)
```

## Troubleshooting

**Import errors:**
```bash
# Verify Python path includes project root
export PYTHONPATH=/path/to/your/project:$PYTHONPATH
```

**Database not created:**
```bash
# Database is created automatically on first log entry
# Check api/skein/data/ for skein.db
ls -la api/skein/data/
```

**Tools not loading:**
```bash
# Tools are auto-discovered from tools/ directory
# Verify tools start with skein_ prefix
ls tools/skein_*.py
```

## Documentation

See `implementia/SKEIN_STRATEGIC_PLAN.md` for full architecture and design decisions.
