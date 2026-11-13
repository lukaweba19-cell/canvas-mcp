"""Tool modules for the Canvas student MCP server."""

from .assignments import register_shared_assignment_tools
from .courses import register_course_tools, register_shared_content_tools
from .discovery import register_discovery_tools
from .discussions import register_shared_discussion_tools
from .files import register_shared_file_tools
from .messaging import register_shared_messaging_tools
from .modules import register_shared_module_tools
from .rubrics import register_rubric_tools
from .self_identity import register_self_identity_tools
from .student_tools import register_student_tools
from .student_write import register_student_write_tools

__all__ = [
    'register_course_tools',
    'register_discovery_tools',
    'register_rubric_tools',
    'register_self_identity_tools',
    'register_shared_assignment_tools',
    'register_shared_content_tools',
    'register_shared_discussion_tools',
    'register_shared_file_tools',
    'register_shared_messaging_tools',
    'register_shared_module_tools',
    'register_student_tools',
    'register_student_write_tools',
]
