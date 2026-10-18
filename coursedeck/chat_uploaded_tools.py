"""Conversation-scoped retrieval of user-uploaded document text."""

from .knowledge import normalized_text, search_terms


class UploadedFileTools:
    def __init__(self, store, conversation_id, expose):
        self.store, self.conversation_id, self.expose = store, conversation_id, expose
        self.files = store.list(conversation_id)
        self.ids = {item["id"] for item in self.files}

    def document(self, key):
        if key not in self.ids:
            raise ValueError("File is not attached to this conversation")
        return self.store.document(key, self.conversation_id)

    def context(self, current_ids, query):
        # Keep the manifest for all files, but bound the initial text sent to the provider.
        selected = current_ids[:5] or [item["id"] for item in self.files[-5:]]
        documents = [self.document(key) for key in selected]
        return {
            "files": [
                {key: item[key] for key in ("id", "name", "pages", "characters", "complete")}
                | {"warning_count": len(item["warnings"])}
                for item in self.files
            ],
            "documents": self.expose(documents, query=query, limit=4000),
            "reading_mode": "Extracted text only; images and diagrams are not read",
        }

    def definitions(self, define):
        if not self.files:
            return []
        return [
            define(
                "search_uploaded_files",
                "Search text in files attached to this conversation, including earlier turns. "
                "Use short keywords or an empty query to list files. Returns excerpts and IDs "
                "for read_document; page with offset. This does not search other chats or sources.",
                {
                    "query": {"type": "string", "maxLength": 2000},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                ["query"],
            )
        ]

    def search(self, arguments):
        query = arguments.get("query", "")
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 8)
        if (
            not isinstance(query, str)
            or len(query) > 2000
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 8
        ):
            raise ValueError("Invalid file search parameters")
        terms = search_terms(query)
        matches = []
        for item in self.files:
            document = self.document(item["id"])
            text = normalized_text(document["title"] + "\n" + document["body"])
            score = sum(weight for term, weight in terms.items() if term in text)
            if not terms or score:
                matches.append((score, document))
        matches.sort(key=lambda item: item[0], reverse=True)
        return {
            "documents": self.expose(
                [item[1] for item in matches[offset : offset + limit]], query=query, limit=2000
            ),
            "total": len(matches),
            "offset": offset,
            "has_more": offset + limit < len(matches),
        }
