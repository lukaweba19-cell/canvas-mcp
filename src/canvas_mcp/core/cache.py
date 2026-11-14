"""Course caching system for Canvas API."""

from .client import fetch_all_paginated_results, make_canvas_request
from .logging import log_error, log_info
from .validation import validate_params

course_code_to_id_cache: dict[str, str] = {}
id_to_course_code_cache: dict[str, str] = {}


async def refresh_course_cache() -> bool:
    """Refresh the global course cache."""
    global course_code_to_id_cache, id_to_course_code_cache

    log_info("Refreshing course cache")
    courses = await fetch_all_paginated_results("/courses", {"per_page": 100})

    if isinstance(courses, dict) and "error" in courses:
        log_error("Error building course cache", error=courses.get("error"))
        return False

    course_code_to_id_cache = {}
    id_to_course_code_cache = {}

    for course in courses:
        course_id = str(course.get("id"))
        course_code = course.get("course_code")

        if course_code and course_id:
            course_code_to_id_cache[course_code] = course_id
            id_to_course_code_cache[course_id] = course_code

    log_info(f"Cached {len(course_code_to_id_cache)} course codes")
    return True


@validate_params
async def get_course_id(course_identifier: str | int) -> str:
    """Get course ID from either course code or ID, with caching.

    Args:
        course_identifier: The course identifier, which can be:
                          - A course code (e.g., 'badm_554_120251_246794')
                          - A numeric course ID (as string or int)
                          - A SIS ID format (e.g., 'sis_course_id:xxx')

    Returns:
        The course ID as a string
    """
    global course_code_to_id_cache, id_to_course_code_cache

    course_str = str(course_identifier)

    if course_str.isdigit():
        return course_str

    if course_str.startswith("sis_course_id:"):
        return course_str

    if course_str in course_code_to_id_cache:
        return course_code_to_id_cache[course_str]

    if "_" in course_str:
        if not course_code_to_id_cache:
            await refresh_course_cache()
            if course_str in course_code_to_id_cache:
                return course_code_to_id_cache[course_str]

        return f"sis_course_id:{course_str}"

    return course_str


async def get_course_code(course_id: str | int) -> str | None:
    """Get course code from ID, with caching."""
    global id_to_course_code_cache, course_code_to_id_cache

    course_id = str(course_id)

    if "_" in course_id:
        return course_id

    if course_id in id_to_course_code_cache:
        return id_to_course_code_cache[course_id]

    if not id_to_course_code_cache:
        await refresh_course_cache()
        if course_id in id_to_course_code_cache:
            return id_to_course_code_cache[course_id]

    response = await make_canvas_request("get", f"/courses/{course_id}")
    if "error" not in response and "course_code" in response:
        code: str | None = response.get("course_code", "")
        if code:
            id_to_course_code_cache[course_id] = code
            course_code_to_id_cache[code] = course_id
        return code

    return course_id
