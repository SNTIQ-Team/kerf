"""Local, paragraph-exact German court decision corpus (SNTIQ Legal MCP).

The corpus lives outside the repository (``~/.local/share/sntiq-legal/courts``
by default): raw captures, content-hashed and gzip-compressed, plus one
SQLite database with an FTS5 index over paragraphs.  ``SCHEMA.md`` documents
the database contract that the Node MCP reads directly.
"""

SCHEMA_VERSION = 4
PARSER_VERSION = "courts-parser/1.7.0"
