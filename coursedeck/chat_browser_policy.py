"""Navigation policy for the Chat reader, independent of model instructions."""

import re
from urllib.parse import parse_qsl, unquote, urlsplit

from .knowledge import safe_source_url

PROVIDERS = {"google_classroom", "brightspace", "gradescope", "webassign"}
WRITE = re.compile(
    r"(?:^|[^a-z])(?:submit|turn.?in|unsubmit|delete|remove|logout|signout|sign.out|"
    r"enroll|unenroll|withdraw|edit|save|send|upload|purchase|accept|start|launch|"
    r"begin|attempt|answer|reset|regrade)(?:$|[^a-z])|提交|删除|退出|发送|保存|开始|购买",
    re.I,
)
CONTROL = re.compile(
    r"(?:next(?: page)?|previous(?: page)?|load more|show more|view more|expand(?: all)?|"
    r"details|instructions|rubric|materials|resources|content|overview|"
    r"classwork|stream|current assignments|past assignments|future assignments|"
    r"下一页|上一页|更多|展开|详情|课程作业|课程材料)",
    re.I,
)
QUERY_KEYS = {
    "db",
    "qi",
    "ou",
    "d2l_body_type",
    "topicId",
    "itemIdentifier",
    "moduleId",
    "isprv",
    "isPopup",
    "page",
    "offset",
    "start",
    "sort",
    "order",
    "filter",
    "hl",
    "authuser",
    "class",
    "classId",
    "classid",
    "section",
    "sectionId",
    "courseId",
    "course",
    "dep",
    "aid",
    "assignmentId",
    "action",
}
BRIGHTSPACE_VIEWS = {
    "/d2l/lms/dropbox/dropbox.d2l",
    "/d2l/lms/dropbox/user/folder_submit_files.d2l",
    "/d2l/lms/dropbox/user/folders_list.d2l",
    "/d2l/lms/quizzing/user/quiz_summary.d2l",
    "/d2l/lms/quizzing/user/quizzes_list.d2l",
    "/d2l/lms/quizzing/quizzing.d2l",
    "/d2l/lms/news/main.d2l",
}

# Assets are a separate channel from course data. Unknown CDNs are deliberately
# not inferred from a page: a page's HTML cannot extend its own network authority.
STATIC_PATHS = {
    "brightspace": ("/d2l/common/", "/d2l/lp/", "/lib/", "/assets/", "/static/"),
    "gradescope": ("/assets/", "/static/"),
    "google_classroom": ("/static/", "/_/scs/classroom-static/"),
    "webassign": ("/assets/", "/static/", "/js/", "/css/", "/wa_files/"),
}
STATIC_HOST_PATHS = {
    "brightspace": {"s.brightspace.com": ("/lib/",)},
    "google_classroom": {
        "www.gstatic.com": ("/classroom/", "/_/mss/boq-classroom/"),
        "ssl.gstatic.com": ("/classroom/",),
    },
}
STATIC_QUERY_KEYS = {"v", "version", "build", "rev", "hash", "lang", "hl"}
API_QUERY_KEYS = {"bookmark", "pageSize", "page", "offset", "limit", "sort", "order"}


def canonical_path(path):
    """Reject encoded separators/traversal instead of guessing server decoding."""
    decoded = unquote(path)
    return not (
        "%" in decoded
        or "\\" in decoded
        or ".." in decoded.split("/")
        or decoded.count("/") != path.count("/")
        or any(ord(char) < 32 for char in decoded)
    )


def clean_url(url):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or "\\" in url
            or any(ord(char) < 32 for char in url)
        ):
            return None
        return safe_source_url(url)
    except (ValueError, TypeError):
        return None


class CourseNavigation:
    def __init__(self, course, task_urls=()):
        self.course = course
        self.root = clean_url(course.get("source_url") or "")
        self.tasks = {clean_url(url) for url in task_urls if url}

    def allows(self, url):
        clean = clean_url(url)
        if not clean or not self.root:
            return False
        p, root = urlsplit(clean), urlsplit(self.root)
        if p.netloc != root.netloc:
            return False
        decoded = unquote(p.path)
        reading_view = self.course["provider"] == "brightspace" and p.path in BRIGHTSPACE_VIEWS
        if not canonical_path(p.path) or WRITE.search(decoded) and not reading_view:
            return False
        pairs = parse_qsl(p.query)
        query = dict(pairs)
        if len(query) != len(pairs):
            return False
        if any(key not in QUERY_KEYS for key in query):
            return False
        if any(WRITE.search(value) for value in query.values()):
            return False
        provider, cid = self.course["provider"], str(self.course["external_id"])
        # A matching path must not smuggle a different course through its query.
        course_keys = {
            "brightspace": {"ou", "courseId"},
            "gradescope": {"courseId", "course"},
            "google_classroom": {"courseId"},
            "webassign": {"class", "classId", "classid", "courseId", "course"},
        }.get(provider, set())
        if any(query[key] != cid for key in course_keys & query.keys()):
            return False
        if provider == "gradescope":
            return bool(
                re.fullmatch(
                    rf"/courses/{re.escape(cid)}(?:/assignments(?:/\d+"
                    r"(?:/(?:submissions/\d+|review|rubric))?)?)?/?",
                    p.path,
                )
            )
        if provider == "google_classroom":
            match = re.search(r"/c/([^/]+)", root.path)
            return bool(match and re.match(rf"/(?:u/\d+/)?c/{re.escape(match[1])}(?:/|$)", p.path))
        if provider == "brightspace":
            path_course = re.match(r"/d2l/(?:home|le/content|le/lessons|le)/(\d+)(?:/|$)", p.path)
            if path_course and path_course[1] != cid:
                return False
            return bool(
                re.match(rf"/d2l/(?:home|le/content)/{re.escape(cid)}(?:/|$)", p.path)
                or re.fullmatch(
                    rf"/d2l/le/lessons/{re.escape(cid)}(?:/(?:topics|units)/\d+)?/?", p.path
                )
                or (reading_view and query.get("ou") == cid)
                or re.match(rf"/d2l/le/{re.escape(cid)}/(?:discussions|news)/", p.path)
                or re.match(rf"/content/enforced/{re.escape(cid)}[-/]", p.path)
            )
        if provider == "webassign":
            # Deployment links carry no course ID: accept only cached task identities.
            if clean in self.tasks or clean == self.root:
                return True
            identity = {k: v for k, v in parse_qsl(root.query) if k != "action"}
            return bool(
                identity
                and p.path == root.path
                and all(query.get(key) == value for key, value in identity.items())
                and query.get("action", "")
                in {"", "home/index", "assignments", "pastassignments", "futureassignments"}
            )
        return False

    def allows_resource(self, url, resource_type):
        """Authorize a subrequest independently of its initiating page.

        Fetch/XHR may read only the selected course. Script/style resources use
        fixed provider asset paths; they cannot impersonate a course API.
        Existing connector-controlled SSO restoration happens before this policy
        is installed and is unaffected by these course-reading restrictions.
        """
        if not self.root or not clean_url(url):
            return False
        parsed, root = urlsplit(url), urlsplit(self.root)
        if not canonical_path(parsed.path):
            return False
        if resource_type in {"script", "stylesheet"}:
            return self._static_resource(parsed, root, resource_type)
        if resource_type not in {"fetch", "xhr", "document"} or parsed.netloc != root.netloc:
            return False
        if self.allows(url):
            return True
        if self.course["provider"] != "brightspace":
            return False
        # These are the course-bound GET families used by the existing connector.
        # Global enrollments, user search and arbitrary API paths are not allowed.
        cid = re.escape(str(self.course["external_id"]))
        if not re.fullmatch(
            rf"/d2l/api/le/\d+\.\d+/{cid}/(?:"
            r"content/(?:toc|myItems|modules/\d+|topics/\d+)|"
            r"news(?:/\d+)?|quizzes(?:/\d+)?|"
            r"discussions/forums(?:/\d+(?:/topics(?:/\d+(?:/posts(?:/\d+)?)?)?)?)?|"
            r"dropbox/folders(?:/\d+(?:/(?:feedback|submissions/mysubmissions))?)?)/?",
            parsed.path,
        ):
            return False
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        return len(dict(pairs)) == len(pairs) and all(
            key in API_QUERY_KEYS and len(value) <= 256 and not WRITE.search(value)
            for key, value in pairs
        )

    def _static_resource(self, parsed, root, resource_type):
        provider = self.course["provider"]
        paths = (
            STATIC_PATHS.get(provider, ())
            if parsed.netloc == root.netloc
            else STATIC_HOST_PATHS.get(provider, {}).get(parsed.hostname, ())
        )
        if not any(parsed.path.startswith(prefix) for prefix in paths):
            return False
        google_bundle = (
            provider == "google_classroom"
            and parsed.hostname == "www.gstatic.com"
            and parsed.path.startswith("/_/mss/boq-classroom/_/")
            and ("/js/" if resource_type == "script" else "/ss/") in parsed.path
        )
        extension = r"\.(?:js|mjs)$" if resource_type == "script" else r"\.css$"
        if not google_bundle and not re.search(extension, parsed.path, re.I):
            return False
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        return len(dict(pairs)) == len(pairs) and all(
            key in STATIC_QUERY_KEYS and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value)
            for key, value in pairs
        )


def permitted_control(item):
    label = item.get("label", "").strip()
    return bool(
        label
        and not WRITE.search(label)
        and (item.get("nativeDisclosure") or CONTROL.fullmatch(label))
        and (item.get("kind") == "disclosure" or item.get("role") in {"button", "tab"})
    )
