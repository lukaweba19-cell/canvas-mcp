"""Rubric-related MCP tools for Canvas API."""

import ast
import csv
import json
import math
from io import StringIO
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import make_canvas_request
from ..core.dates import format_date, truncate_text
from ..core.untrusted_content import (
    fence_untrusted,
    fence_untrusted_inline,
)
from ..core.validation import validate_params
from ..core.write_confirmation import (
    ConfirmationGuard,
    unconfirmed_write_warning,
)

_RUBRIC_UPDATE_GUARD = ConfirmationGuard(
    nothing_done="Nothing was updated.",
)


def preprocess_criteria_string(criteria_string: str) -> str:
    """Preprocess criteria string to handle common formatting issues.

    Args:
        criteria_string: Raw criteria string that might have formatting issues

    Returns:
        Cleaned criteria string ready for JSON parsing
    """
    # Strip whitespace
    cleaned = criteria_string.strip()

    # Handle cases where quotes might be escaped incorrectly
    # This is a common issue with string serialization
    if cleaned.startswith('"{') and cleaned.endswith('}"'):
        # Remove outer quotes and unescape inner quotes
        cleaned = cleaned[1:-1].replace('\\"', '"').replace('\\\\', '\\')

    return cleaned


def validate_rubric_criteria(criteria_json: str) -> dict[str, Any]:
    """Validate and parse rubric criteria JSON structure.

    Args:
        criteria_json: JSON string containing rubric criteria

    Returns:
        Parsed criteria dictionary

    Raises:
        ValueError: If JSON is invalid or structure is incorrect
    """
    # Preprocess the string to handle common issues
    cleaned_json = preprocess_criteria_string(criteria_json)

    try:
        criteria = json.loads(cleaned_json)
    except json.JSONDecodeError as e:
        # Try alternative parsing methods if JSON fails
        try:
            # Maybe it's a Python literal string representation
            criteria = ast.literal_eval(cleaned_json)
            if isinstance(criteria, dict):
                # Successfully parsed as Python literal, continue with validation
                pass
            else:
                raise ValueError("Parsed result is not a dictionary")
        except (ValueError, SyntaxError):
            # Both JSON and literal_eval failed, provide detailed error
            error_msg = f"Invalid JSON format: {str(e)}\n"
            error_msg += f"Original string length: {len(criteria_json)}\n"
            error_msg += f"Cleaned string length: {len(cleaned_json)}\n"
            error_msg += f"First 200 characters of original: {repr(criteria_json[:200])}\n"
            error_msg += f"First 200 characters of cleaned: {repr(cleaned_json[:200])}\n"
            if len(cleaned_json) > 200:
                error_msg += f"Last 100 characters of cleaned: {repr(cleaned_json[-100:])}"
            error_msg += "\nAlso failed to parse as Python literal. Please ensure the criteria is valid JSON."
            raise ValueError(error_msg) from e

    if not isinstance(criteria, dict):
        raise ValueError("Criteria must be a JSON object (dictionary)")

    # Validate each criterion
    for criterion_key, criterion_data in criteria.items():
        if not isinstance(criterion_data, dict):
            raise ValueError(f"Criterion {criterion_key} must be an object")

        if "description" not in criterion_data:
            raise ValueError(f"Criterion {criterion_key} must have a 'description' field")

        if "points" not in criterion_data:
            raise ValueError(f"Criterion {criterion_key} must have a 'points' field")

        try:
            points = float(criterion_data["points"])
            if points < 0:
                raise ValueError(f"Criterion {criterion_key} points must be non-negative")
        except (ValueError, TypeError) as err:
            raise ValueError(f"Criterion {criterion_key} points must be a valid number") from err

        # Validate ratings if present - handle both object and array formats
        if "ratings" in criterion_data:
            ratings = criterion_data["ratings"]

            # Handle both object and array formats
            if isinstance(ratings, dict):
                # Object format: {"1": {...}, "2": {...}}
                for rating_key, rating_data in ratings.items():
                    if not isinstance(rating_data, dict):
                        raise ValueError(f"Rating {rating_key} in criterion {criterion_key} must be an object")

                    if "description" not in rating_data:
                        raise ValueError(f"Rating {rating_key} in criterion {criterion_key} must have a 'description' field")

                    if "points" not in rating_data:
                        raise ValueError(f"Rating {rating_key} in criterion {criterion_key} must have a 'points' field")

                    try:
                        rating_points = float(rating_data["points"])
                        if rating_points < 0:
                            raise ValueError(f"Rating {rating_key} points must be non-negative")
                    except (ValueError, TypeError) as err:
                        raise ValueError(f"Rating {rating_key} points must be a valid number") from err

            elif isinstance(ratings, list):
                # Array format: [{"description": ..., "points": ...}, ...]
                for i, rating_data in enumerate(ratings):
                    if not isinstance(rating_data, dict):
                        raise ValueError(f"Rating {i} in criterion {criterion_key} must be an object")

                    if "description" not in rating_data:
                        raise ValueError(f"Rating {i} in criterion {criterion_key} must have a 'description' field")

                    if "points" not in rating_data:
                        raise ValueError(f"Rating {i} in criterion {criterion_key} must have a 'points' field")

                    try:
                        rating_points = float(rating_data["points"])
                        if rating_points < 0:
                            raise ValueError(f"Rating {i} points must be non-negative")
                    except (ValueError, TypeError) as err:
                        raise ValueError(f"Rating {i} points must be a valid number") from err

            else:
                raise ValueError(f"Criterion {criterion_key} ratings must be an object or array")

    return criteria


def validate_rubric_update_criteria(
    criteria_json: str,
    current_criteria: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate a full rubric replacement without permitting ID churn.

    Canvas replaces the complete criteria array on update. Missing criterion or
    rating IDs can silently mint new IDs and orphan existing assessment data, so
    updates require an exact, ID-keyed copy of the current structure.
    """
    criteria = validate_rubric_criteria(criteria_json)
    if not criteria:
        raise ValueError("criteria must contain at least one criterion")

    current_by_id: dict[str, dict[str, Any]] = {}
    for criterion in current_criteria:
        criterion_id = criterion.get("id")
        if criterion_id is None:
            raise ValueError("Canvas returned a criterion without an ID")
        criterion_id_str = str(criterion_id)
        if criterion_id_str in current_by_id:
            raise ValueError(
                f"Canvas returned duplicate criterion ID {criterion_id_str!r}"
            )
        current_by_id[criterion_id_str] = criterion

    if set(criteria) != set(current_by_id):
        raise ValueError(
            "criteria must include the complete existing criterion ID set; "
            "adding or removing criteria is not supported by this guarded tool"
        )

    for criterion_id, proposed in criteria.items():
        if str(proposed.get("id", "")) != criterion_id:
            raise ValueError(
                f"criterion {criterion_id} must include id={criterion_id!r}"
            )
        if not isinstance(proposed.get("description"), str):
            raise ValueError(
                f"criterion {criterion_id} description must be a string"
            )

        # These Canvas flags affect scoring/range behavior but are not editable
        # through this deliberately narrow tool. Carry them forward so a text
        # or points edit cannot silently reset existing behavior.
        for field in ("criterion_use_range", "ignore_for_scoring"):
            if field in current_by_id[criterion_id]:
                proposed[field] = current_by_id[criterion_id][field]
        if "long_description" not in proposed:
            proposed["long_description"] = (
                current_by_id[criterion_id].get("long_description") or ""
            )
        if not isinstance(proposed["long_description"], str):
            raise ValueError(
                f"criterion {criterion_id} long_description must be a string"
            )

        ratings = proposed.get("ratings")
        if not isinstance(ratings, dict):
            raise ValueError(
                f"criterion {criterion_id} ratings must be an ID-keyed object"
            )

        current_ratings = current_by_id[criterion_id].get("ratings", [])
        current_rating_by_id: dict[str, dict[str, Any]] = {}
        for rating in current_ratings:
            rating_id = rating.get("id") if isinstance(rating, dict) else None
            if rating_id is None:
                raise ValueError(
                    f"Canvas returned a rating without an ID in criterion {criterion_id}"
                )
            rating_id_str = str(rating_id)
            if rating_id_str in current_rating_by_id:
                raise ValueError(
                    f"Canvas returned duplicate rating ID {rating_id_str!r} "
                    f"in criterion {criterion_id}"
                )
            current_rating_by_id[rating_id_str] = rating

        if set(ratings) != set(current_rating_by_id):
            raise ValueError(
                f"criterion {criterion_id} must include the complete existing "
                "rating ID set; adding or removing ratings is not supported"
            )
        for rating_id, rating in ratings.items():
            if str(rating.get("id", "")) != rating_id:
                raise ValueError(
                    f"rating {rating_id} in criterion {criterion_id} must include "
                    f"id={rating_id!r}"
                )
            if not isinstance(rating.get("description"), str):
                raise ValueError(
                    f"rating {rating_id} in criterion {criterion_id} "
                    "description must be a string"
                )
            if "long_description" not in rating:
                rating["long_description"] = (
                    current_rating_by_id[rating_id].get("long_description") or ""
                )
            if not isinstance(rating["long_description"], str):
                raise ValueError(
                    f"rating {rating_id} in criterion {criterion_id} "
                    "long_description must be a string"
                )

    ordered_criteria: dict[str, Any] = {}
    for current_criterion in current_criteria:
        criterion_id = str(current_criterion["id"])
        proposed = criteria[criterion_id]
        proposed_ratings = proposed["ratings"]
        proposed["ratings"] = {
            str(current_rating["id"]): proposed_ratings[str(current_rating["id"])]
            for current_rating in current_criterion.get("ratings", [])
        }
        ordered_criteria[criterion_id] = proposed

    return ordered_criteria


def _rubric_update_state(
    response: Any,
    rubric_id: str,
    rubric_association_id: str,
) -> dict[str, Any]:
    """Return the current guarded state or raise when identity is ambiguous."""
    if not isinstance(response, dict) or "error" in response:
        detail = (
            response.get("error") if isinstance(response, dict) else "invalid response"
        )
        raise ValueError(f"could not fetch the rubric: {detail}")
    if str(response.get("id")) != rubric_id:
        raise ValueError("Canvas returned a different rubric ID")
    if response.get("read_only") is True:
        raise ValueError("this rubric is read-only")

    criteria = response.get("data")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("Canvas returned no complete rubric criteria")

    associations = response.get("associations")
    if not isinstance(associations, list):
        raise ValueError("Canvas returned no association data")
    matches = [
        association
        for association in associations
        if isinstance(association, dict)
        and str(association.get("id")) == rubric_association_id
        and str(association.get("rubric_id")) == rubric_id
    ]
    if len(matches) != 1:
        raise ValueError(
            "the requested rubric association was not returned exactly once"
        )

    return {
        "id": response.get("id"),
        "title": response.get("title"),
        "free_form_criterion_comments": response.get("free_form_criterion_comments"),
        "data": criteria,
        "association": matches[0],
    }


def _rubric_update_fingerprint(
    current: dict[str, Any],
    title: str,
    criteria: dict[str, Any],
    free_form_criterion_comments: bool,
) -> str:
    return _RUBRIC_UPDATE_GUARD.fingerprint(
        json.dumps(current, sort_keys=True, separators=(",", ":"), default=str),
        title,
        json.dumps(criteria, sort_keys=True, separators=(",", ":"), default=str),
        "1" if free_form_criterion_comments else "0",
    )


def _render_rubric_update_preview(
    current: dict[str, Any],
    title: str,
    criteria: dict[str, Any],
    free_form_criterion_comments: bool,
) -> str:
    lines = [
        f"Rubric ID: {current['id']}",
        f"Association ID: {current['association']['id']}",
        f"Current title: {fence_untrusted_inline(str(current.get('title') or ''), 'rubric title')}",
        f"New title: {title}",
        "Replacement criteria (complete ID-preserving set):",
    ]
    current_criteria = {str(item["id"]): item for item in current["data"]}
    for criterion_id, criterion in criteria.items():
        lines.append(
            f"- {criterion_id}: {criterion['description']} ({float(criterion['points']):g} pts)"
        )
        previous = current_criteria[criterion_id]
        lines.append("  Current long description: " + fence_untrusted(
            previous.get("long_description") or "(empty)", "criterion long description"
        ))
        lines.append("  New long description: " + fence_untrusted(
            criterion.get("long_description") or "(empty)", "proposed criterion long description"
        ) if criterion.get("long_description") else "  New long description: (empty)")
        for field in ("criterion_use_range", "ignore_for_scoring"):
            if field in criterion:
                lines.append(f"  {field} (preserved): {criterion[field]}")
        current_ratings = {str(item["id"]): item for item in previous["ratings"]}
        for rating_id, rating in criterion["ratings"].items():
            lines.append(
                f"  - {rating_id}: {rating['description']} "
                f"({float(rating['points']):g} pts)"
            )
            lines.append("    Current long description: " + fence_untrusted(
                current_ratings[rating_id].get("long_description") or "(empty)",
                "rating long description",
            ))
            lines.append("    New long description: " + fence_untrusted(
                rating["long_description"], "proposed rating long description"
            ) if rating.get("long_description") else "    New long description: (empty)")
    lines.append("Assignment points possible: preserved")
    lines.append(
        "Free-form criterion comments: "
        + ("enabled" if free_form_criterion_comments else "disabled")
    )
    return "\n".join(lines)


def build_rubric_update_form_data(
    title: str,
    criteria: dict[str, Any],
    rubric_association_id: str,
    free_form_criterion_comments: bool,
) -> dict[str, str]:
    """Encode a full ID-preserving rubric replacement for Canvas."""
    form_data = {
        "rubric_association_id": rubric_association_id,
        "rubric[skip_updating_points_possible]": "1",
        "rubric[title]": title,
        "rubric[free_form_criterion_comments]": (
            "1" if free_form_criterion_comments else "0"
        ),
    }
    for criterion_index, (criterion_id, criterion) in enumerate(criteria.items()):
        prefix = f"rubric[criteria][{criterion_index}]"
        form_data[f"{prefix}[id]"] = criterion_id
        form_data[f"{prefix}[description]"] = str(criterion["description"])
        form_data[f"{prefix}[long_description]"] = str(
            criterion.get("long_description") or ""
        )
        form_data[f"{prefix}[points]"] = str(float(criterion["points"]))
        for field in ("criterion_use_range", "ignore_for_scoring"):
            if field in criterion:
                form_data[f"{prefix}[{field}]"] = "1" if criterion[field] else "0"

        for rating_index, (rating_id, rating) in enumerate(
            criterion["ratings"].items()
        ):
            rating_prefix = f"{prefix}[ratings][{rating_index}]"
            form_data[f"{rating_prefix}[id]"] = rating_id
            form_data[f"{rating_prefix}[description]"] = str(rating["description"])
            form_data[f"{rating_prefix}[long_description]"] = str(
                rating.get("long_description") or ""
            )
            form_data[f"{rating_prefix}[points]"] = str(float(rating["points"]))
    return form_data


def _update_response_has_expected_identity(
    response: Any,
    rubric_id: str,
    rubric_association_id: str,
) -> bool:
    if not isinstance(response, dict):
        return False
    rubric = response.get("rubric")
    association = response.get("rubric_association")
    return (
        isinstance(rubric, dict)
        and str(rubric.get("id")) == rubric_id
        and isinstance(association, dict)
        and str(association.get("id")) == rubric_association_id
        and str(association.get("rubric_id")) == rubric_id
    )


def _rubric_matches_requested_update(
    current: dict[str, Any],
    title: str,
    criteria: dict[str, Any],
    free_form_criterion_comments: bool,
) -> bool:
    if current.get("title") != title:
        return False
    if (
        bool(current.get("free_form_criterion_comments"))
        != free_form_criterion_comments
    ):
        return False

    returned = {
        str(criterion.get("id")): criterion
        for criterion in current["data"]
        if isinstance(criterion, dict) and criterion.get("id") is not None
    }
    if len(returned) != len(current["data"]) or set(returned) != set(criteria):
        return False
    try:
        for criterion_id, requested in criteria.items():
            actual = returned[criterion_id]
            if actual.get("description") != requested.get("description"):
                return False
            if (actual.get("long_description") or "") != (
                requested.get("long_description") or ""
            ):
                return False
            for field in ("criterion_use_range", "ignore_for_scoring"):
                if field in requested and bool(actual.get(field)) != bool(
                    requested[field]
                ):
                    return False
            actual_points = actual.get("points")
            requested_points = requested.get("points")
            if (
                actual_points is None
                or requested_points is None
                or not math.isclose(
                    float(actual_points),
                    float(requested_points),
                    rel_tol=1e-9,
                    abs_tol=1e-6,
                )
            ):
                return False

            raw_actual_ratings = actual.get("ratings")
            if not isinstance(raw_actual_ratings, list):
                return False
            actual_ratings = {
                str(rating.get("id")): rating
                for rating in raw_actual_ratings
                if isinstance(rating, dict) and rating.get("id") is not None
            }
            requested_ratings = requested["ratings"]
            if len(actual_ratings) != len(raw_actual_ratings) or set(
                actual_ratings
            ) != set(requested_ratings):
                return False
            for rating_id, requested_rating in requested_ratings.items():
                actual_rating = actual_ratings[rating_id]
                if actual_rating.get("description") != requested_rating.get(
                    "description"
                ):
                    return False
                if (actual_rating.get("long_description") or "") != (
                    requested_rating.get("long_description") or ""
                ):
                    return False
                actual_rating_points = actual_rating.get("points")
                requested_rating_points = requested_rating.get("points")
                if (
                    actual_rating_points is None
                    or requested_rating_points is None
                    or not math.isclose(
                        float(actual_rating_points),
                        float(requested_rating_points),
                        rel_tol=1e-9,
                        abs_tol=1e-6,
                    )
                ):
                    return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def format_rubric_response(response: dict[str, Any]) -> str:
    """Format Canvas API rubric response into readable text.

    Args:
        response: Canvas API response (may be non-standard format)

    Returns:
        Formatted string representation of the rubric
    """
    # Handle Canvas API's non-standard response format
    if "rubric" in response and "rubric_association" in response:
        rubric = response["rubric"]
        association = response["rubric_association"]

        result = "Rubric Created/Updated Successfully!\n\n"
        result += "Rubric Details:\n"
        result += f"  ID: {rubric.get('id', 'N/A')}\n"
        result += f"  Title: {rubric.get('title', 'Untitled')}\n"
        result += f"  Context: {rubric.get('context_type', 'N/A')} (ID: {rubric.get('context_id', 'N/A')})\n"
        result += f"  Points Possible: {rubric.get('points_possible', 0)}\n"
        result += f"  Reusable: {'Yes' if rubric.get('reusable', False) else 'No'}\n"
        result += f"  Free Form Comments: {'Yes' if rubric.get('free_form_criterion_comments', False) else 'No'}\n"

        if association:
            result += "\nAssociation Details:\n"
            result += f"  Associated with: {association.get('association_type', 'N/A')} (ID: {association.get('association_id', 'N/A')})\n"
            result += f"  Used for Grading: {'Yes' if association.get('use_for_grading', False) else 'No'}\n"
            result += f"  Purpose: {association.get('purpose', 'N/A')}\n"

        # Show criteria count
        data = rubric.get('data', [])
        if data:
            result += f"\nCriteria: {len(data)} criterion defined\n"

        return result

    # Handle standard rubric response
    else:
        result = "Rubric Operation Completed!\n\n"
        result += f"ID: {response.get('id', 'N/A')}\n"
        result += f"Title: {response.get('title', 'Untitled')}\n"
        result += f"Points Possible: {response.get('points_possible', 0)}\n"
        return result


def rubric_grade_is_confirmed(
    assignment: dict[str, Any], assessment: dict[str, Any], response: dict[str, Any]
) -> bool:
    """Confirm the returned score for a complete rubric, excluding outcome-only criteria.

    Partial assessments or missing rubric metadata cannot establish the expected
    score. A matching score confirms the outcome, not a change or publication.
    """
    criteria = assignment.get("rubric")
    if not isinstance(criteria, list) or not criteria:
        return False
    try:
        ids = {str(c["id"]) for c in criteria}
        if ids != set(assessment):
            return False
        expected = sum(
            float(assessment[str(c["id"])]["points"])
            for c in criteria if not c.get("ignore_for_scoring", False)
        )
        score = response.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return False
        return (
            response.get("grade") is not None
            and math.isfinite(expected) and math.isfinite(score)
            and math.isclose(score, expected, rel_tol=1e-9, abs_tol=1e-6)
        )
    except (KeyError, TypeError, ValueError):
        return False


RUBRIC_GRADE_UNCONFIRMED = (
    "Rubric grade unconfirmed: the assessment may have been saved, but Canvas's "
    "returned grade/score could not be verified against the complete rubric. "
    "Check the submission in Canvas before retrying. No explicit grade was forced."
)


def build_rubric_assessment_form_data(
    rubric_assessment: dict[str, Any],
    comment: str | None = None
) -> dict[str, str]:
    """Convert rubric assessment dict to Canvas form-encoded format.

    Canvas API expects rubric assessment data as form-encoded parameters with
    bracket notation: rubric_assessment[criterion_id][field]=value

    Args:
        rubric_assessment: Dict mapping criterion IDs to assessment data
                          Format: {"criterion_id": {"points": X, "rating_id": Y, "comments": Z}}
        comment: Optional overall comment for the submission

    Returns:
        Flattened dict with Canvas bracket notation keys

    Example:
        Input: {"_8027": {"points": 2, "rating_id": "blank", "comments": "Great work"}}
        Output: {
            "rubric_assessment[_8027][points]": "2",
            "rubric_assessment[_8027][rating_id]": "blank",
            "rubric_assessment[_8027][comments]": "Great work"
        }
    """
    form_data: dict[str, str] = {}

    # Transform rubric_assessment object into Canvas's form-encoded format
    for criterion_id, assessment in rubric_assessment.items():
        # Points are required
        if "points" in assessment:
            form_data[f"rubric_assessment[{criterion_id}][points]"] = str(assessment["points"])

        # Rating ID is optional but recommended
        if "rating_id" in assessment:
            form_data[f"rubric_assessment[{criterion_id}][rating_id]"] = str(assessment["rating_id"])

        # Comments are optional
        if "comments" in assessment:
            form_data[f"rubric_assessment[{criterion_id}][comments]"] = str(assessment["comments"])

    # Add optional overall comment
    if comment:
        form_data["comment[text_comment]"] = comment

    return form_data


def build_rubric_create_form_data(
    title: str,
    criteria: dict[str, Any],
    assignment_id: str | int | None = None,
    use_for_grading: bool = False,
    reusable: bool = False,
    free_form_criterion_comments: bool = False,
    course_id: str | int | None = None,
) -> dict[str, str]:
    """Build bracket-notation form data for Canvas rubric creation API.

    Canvas POST /courses/:id/rubrics expects indexed hashes for criteria and
    nested ratings. This function encodes them as bracket-notation form data,
    producing the flat key/value dict that
    ``make_canvas_request`` sends when ``use_form_data=True``.

    Args:
        title: Rubric title
        criteria: Validated criteria dict (from validate_rubric_criteria)
        assignment_id: Optional assignment ID to associate the rubric with
        use_for_grading: Whether the rubric should be used for grade calculation
        reusable: Whether the rubric is reusable across courses
        free_form_criterion_comments: Allow free-form comments per criterion
        course_id: Course to bookmark the rubric into when no assignment_id is
            given. Required for the rubric to appear in the Canvas Rubrics UI.

    Returns:
        Flat dict with Canvas bracket-notation keys, all values as strings.

    Example output keys::

        rubric[title]
        rubric[criteria][0][description]
        rubric[criteria][0][points]
        rubric[criteria][0][ratings][0][description]
        rubric[criteria][0][ratings][0][points]
        rubric_association[association_id]        (only when assignment_id given)
        rubric_association[association_type]      (only when assignment_id given)
        rubric_association[use_for_grading]       (only when assignment_id given)
        rubric_association[purpose]               (only when assignment_id given)
    """
    form_data: dict[str, str] = {}

    form_data["rubric[title]"] = title
    form_data["rubric[reusable]"] = "1" if reusable else "0"
    form_data["rubric[free_form_criterion_comments]"] = "1" if free_form_criterion_comments else "0"

    for crit_idx, (_crit_key, criterion_data) in enumerate(criteria.items()):
        prefix = f"rubric[criteria][{crit_idx}]"

        form_data[f"{prefix}[description]"] = str(criterion_data["description"])
        form_data[f"{prefix}[points]"] = str(float(criterion_data["points"]))

        long_desc = criterion_data.get("long_description", "")
        if long_desc:
            form_data[f"{prefix}[long_description]"] = str(long_desc)

        ratings = criterion_data.get("ratings", [])

        # Normalize to a list of dicts (criteria support both list and dict formats)
        if isinstance(ratings, dict):
            # Dict-format ratings use arbitrary user-provided keys that Canvas does not
            # recognise — only description/points/long_description matter.  Discard keys.
            ratings_list = list(ratings.values())
        else:
            ratings_list = list(ratings)

        # Sort ratings highest → lowest so Canvas displays them correctly
        ratings_list = sorted(
            ratings_list,
            key=lambda r: float(r.get("points", 0)),
            reverse=True,
        )

        for rating_idx, rating_data in enumerate(ratings_list):
            rprefix = f"{prefix}[ratings][{rating_idx}]"
            form_data[f"{rprefix}[description]"] = str(rating_data["description"])
            form_data[f"{rprefix}[points]"] = str(float(rating_data["points"]))
            rating_long_desc = rating_data.get("long_description", "")
            if rating_long_desc:
                form_data[f"{rprefix}[long_description]"] = str(rating_long_desc)

    # Canvas needs a rubric_association, not just the rubric. Without one it
    # creates the rubric row in the course context (so GET /courses/:id/rubrics
    # returns it) but no association row, and the Canvas Rubrics UI lists
    # rubrics via the course's *bookmarked* associations. The result is a rubric
    # that is visible over the API and invisible in the interface, which is
    # exactly what #180 reported.
    if assignment_id is not None:
        form_data["rubric_association[association_id]"] = str(assignment_id)
        form_data["rubric_association[association_type]"] = "Assignment"
        form_data["rubric_association[use_for_grading]"] = "1" if use_for_grading else "0"
        form_data["rubric_association[purpose]"] = "grading"
    elif course_id is not None:
        form_data["rubric_association[association_id]"] = str(course_id)
        form_data["rubric_association[association_type]"] = "Course"
        form_data["rubric_association[purpose]"] = "bookmark"
        form_data["rubric_association[bookmarked]"] = "1"
        form_data["rubric_association[use_for_grading]"] = "0"

    return form_data


def rubric_association_id(response: Any) -> Any | None:
    """Return the id of a rubric association Canvas actually created, or None.

    Canvas answers ``200`` for rubric writes whose association parameters it
    silently ignored, so an HTTP success is not evidence that an association
    exists. The only reliable evidence is an id in the payload. This has now
    bitten three separate call sites — #180 (create, invisible in the Rubrics
    UI), #181 (assignment association never attached), and the CSV import path
    (#190) — so the shapes Canvas uses are recognised in exactly one place
    rather than re-derived per tool.

    Handles the three payload shapes:

    - ``{"rubric": {...}, "rubric_association": {"id": N, ...}}`` — rubric create
    - ``{"id": N, "association_type": "...", ...}`` — POST /rubric_associations
    - ``{"rubric_association": null}`` / ``{}`` — accepted, created nothing
    """
    if not isinstance(response, dict):
        return None

    association = response.get("rubric_association")
    if isinstance(association, dict):
        return association.get("id") or None

    # POST /courses/:id/rubric_associations returns the association itself.
    if response.get("association_type") or response.get("association_id"):
        return response.get("id") or None

    return None




def count_csv_rubrics(csv_content: str) -> int | None:
    """Count distinct rubric names in a Canvas rubric CSV.

    Returns None when the CSV cannot be parsed or does not include a
    ``Rubric Name`` column.
    """
    try:
        reader = csv.DictReader(StringIO(csv_content))
    except csv.Error:
        return None

    if not reader.fieldnames:
        return None

    rubric_name_field = next(
        (name for name in reader.fieldnames if isinstance(name, str) and name.strip().lower() == "rubric name"),
        None,
    )
    if not rubric_name_field:
        return None

    names: set[str] = set()
    for row in reader:
        if not isinstance(row, dict):
            # Defensive guard kept deliberately: typeshed says DictReader always
            # yields dicts, so mypy sees this as dead, but a malformed csv module
            # substitute would otherwise crash the loop.
            continue  # type: ignore[unreachable]
        raw_name = row.get(rubric_name_field)
        if isinstance(raw_name, str) and raw_name.strip():
            names.add(raw_name.strip())

    return len(names)


async def _ensure_course_bookmark(response: Any, course_id: str | int) -> str:
    """Guarantee a created rubric is actually bookmarked into the course.

    Canvas may return the rubric with ``rubric_association: null`` even when the
    create request carried association fields. A rubric in that state is
    returned by ``GET /courses/:id/rubrics`` but does not appear in the Canvas
    Rubrics UI, so reporting plain success would be misleading (#180).

    Returns a line to append to the tool output.
    """
    if rubric_association_id(response):
        return ""

    rubric = (response or {}).get("rubric") or {}
    rubric_id = rubric.get("id") or (response or {}).get("id")
    if not rubric_id:
        return "\n" + unconfirmed_write_warning(
            "this rubric was added to the course's Rubrics list",
            {},
            "Canvas returned no rubric ID to retry with. Check Canvas.",
        )

    retry = await make_canvas_request(
        "post",
        f"/courses/{course_id}/rubric_associations",
        data={
            "rubric_association[rubric_id]": str(rubric_id),
            "rubric_association[association_id]": str(course_id),
            "rubric_association[association_type]": "Course",
            "rubric_association[purpose]": "bookmark",
            "rubric_association[bookmarked]": "1",
        },
        use_form_data=True,
    )
    if isinstance(retry, dict) and "error" in retry:
        return (
            f"\n⚠️  The rubric was created but could not be added to the course's "
            f"Rubrics list ({retry['error']}). It exists via the API but will not "
            f"appear in the Canvas Rubrics tool until it is bookmarked.\n"
        )
    return "\nAdded to the course's Rubrics list.\n"


def register_rubric_tools(mcp: FastMCP) -> None:
    """Register all rubric-related MCP tools."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_rubric(course_identifier: str | int,
                         rubric_id: str | int | None = None,
                         assignment_id: str | int | None = None) -> str:
        """Get detailed rubric criteria, ratings, and points.

        Accepts either rubric_id or assignment_id (at least one required).
        If both provided, uses rubric_id (more specific).

        Args:
            course_identifier: Course code or Canvas ID
            rubric_id: Canvas rubric ID (direct lookup)
            assignment_id: Canvas assignment ID (get rubric attached to assignment)
        """
        if rubric_id is None and assignment_id is None:
            return (
                "Error: You must provide either rubric_id or assignment_id.\n\n"
                "Usage:\n"
                "  - get_rubric(course, rubric_id=123) — look up rubric directly\n"
                "  - get_rubric(course, assignment_id=456) — get rubric attached to an assignment\n"
                "\nUse list_rubrics to find rubric IDs for a course."
            )

        course_id = await get_course_id(course_identifier)
        course_display = await get_course_code(course_id) or course_identifier

        # Path 1: Look up by rubric_id (preferred when both provided)
        if rubric_id is not None:
            rubric_id_str = str(rubric_id)

            response = await make_canvas_request(
                "get",
                f"/courses/{course_id}/rubrics/{rubric_id_str}",
                params={"include[]": ["assessments", "associations"]}
            )

            if "error" in response:
                return f"Error fetching rubric: {response['error']}"

            title = response.get("title", "Untitled Rubric")
            points_possible = response.get("points_possible", 0)
            reusable = response.get("reusable", False)
            read_only = response.get("read_only", False)
            data = response.get("data", [])

            # Rubric title, criterion/rating descriptions are author-authored
            # (issue 239).
            result = f"Rubric {fence_untrusted_inline(title, 'rubric title')} in Course {course_display}:\n\n"
            result += f"Rubric ID: {rubric_id}\n"
            result += f"Total Points: {points_possible}\n"
            result += f"Reusable: {'Yes' if reusable else 'No'}\n"
            result += f"Read Only: {'Yes' if read_only else 'No'}\n"

            associations = response.get("associations", [])
            if associations:
                result += "Rubric Associations:\n"
                for association in associations:
                    if not isinstance(association, dict):
                        continue
                    result += (
                        f"  Rubric Association ID: {association.get('id', 'N/A')}\n"
                    )
                    association_type = str(
                        association.get("association_type", "Unknown")
                    )
                    association_label = (
                        "Assignment ID"
                        if association_type == "Assignment"
                        else "Associated Object ID"
                    )
                    result += (
                        f"  {association_label}: "
                        f"{association.get('association_id', 'N/A')}\n"
                    )
                    result += (
                        "  Association Type: "
                        f"{fence_untrusted_inline(association_type, 'rubric association type')}\n"
                    )

            if data:
                result += f"Number of Criteria: {len(data)}\n\n"
                result += "Criteria and Ratings:\n"
                result += "=" * 50 + "\n"

                for i, criterion in enumerate(data, 1):
                    criterion_id = criterion.get("id", "N/A")
                    description = criterion.get("description", "No description")
                    long_description = criterion.get("long_description", "")
                    points = criterion.get("points", 0)
                    ratings = criterion.get("ratings", [])

                    result += f"\nCriterion #{i}: {fence_untrusted_inline(description, 'rubric criterion description')}\n"
                    result += f"  ID: {criterion_id}\n"
                    result += f"  Points: {points}\n"

                    if long_description and long_description != description:
                        result += f"  Description: {fence_untrusted_inline(truncate_text(long_description, 200), 'rubric criterion description')}\n"

                    if ratings:
                        sorted_ratings = sorted(ratings, key=lambda x: x.get("points", 0), reverse=True)
                        for rating in sorted_ratings:
                            rating_desc = rating.get("description", "No description")
                            rating_points = rating.get("points", 0)
                            rating_id = rating.get("id", "N/A")
                            result += f"  - {rating_points} pts: {fence_untrusted_inline(rating_desc, 'rubric rating description')} [ID: {rating_id}]\n"

                            rating_long_desc = rating.get("long_description", "")
                            if rating_long_desc and rating_long_desc != rating_desc:
                                result += f"    {fence_untrusted_inline(truncate_text(rating_long_desc, 100), 'rubric rating description')}\n"

                    result += "\n"
            else:
                result += "\nNo criteria defined for this rubric.\n"

            return result

        # Path 2: Look up via assignment_id
        assignment_id_str = str(assignment_id)

        response = await make_canvas_request(
            "get",
            f"/courses/{course_id}/assignments/{assignment_id_str}",
            params={"include[]": ["rubric", "rubric_settings"]}
        )

        if "error" in response:
            return f"Error fetching rubric: {response['error']}"

        rubric = response.get("rubric")
        if not rubric:
            assignment_name = response.get("name", "Unknown Assignment")
            return (
                "No rubric found for assignment "
                f"{fence_untrusted_inline(assignment_name, 'assignment name')} "
                f"in course {course_display}."
            )

        assignment_name = response.get("name", "Unknown Assignment")
        rubric_settings = response.get("rubric_settings", {})
        use_rubric_for_grading = response.get("use_rubric_for_grading", False)

        result = (
            "Rubric for Assignment "
            f"{fence_untrusted_inline(assignment_name, 'assignment name')} "
            f"in Course {course_display}:\n\n"
        )

        # Grading config (only available via assignment path)
        result += "Grading Config:\n"
        result += f"  Used for Grading: {'Yes' if use_rubric_for_grading else 'No'}\n"
        if rubric_settings:
            result += f"  Points Possible: {rubric_settings.get('points_possible', 'N/A')}\n"
        result += f"Number of Criteria: {len(rubric)}\n\n"

        # Criteria and ratings
        result += "Criteria and Ratings:\n"
        result += "=" * 50 + "\n"

        total_points = 0
        for i, criterion in enumerate(rubric, 1):
            criterion_id = criterion.get("id", "N/A")
            description = criterion.get("description", "No description")
            long_description = criterion.get("long_description", "")
            points = criterion.get("points", 0)
            ratings = criterion.get("ratings", [])

            result += f"\nCriterion #{i}: {fence_untrusted_inline(description, 'rubric criterion description')}\n"
            result += f"  ID: {criterion_id}\n"
            result += f"  Points: {points}\n"

            if long_description and long_description != description:
                result += f"  Description: {fence_untrusted_inline(truncate_text(long_description, 200), 'rubric criterion description')}\n"

            if ratings:
                sorted_ratings = sorted(ratings, key=lambda x: x.get("points", 0), reverse=True)
                for rating in sorted_ratings:
                    rating_desc = rating.get("description", "No description")
                    rating_points = rating.get("points", 0)
                    rating_id = rating.get("id", "N/A")
                    result += f"  - {rating_points} pts: {fence_untrusted_inline(rating_desc, 'rubric rating description')} [ID: {rating_id}]\n"

                    rating_long_desc = rating.get("long_description", "")
                    if rating_long_desc and rating_long_desc != rating_desc:
                        result += f"    {fence_untrusted_inline(truncate_text(rating_long_desc, 100), 'rubric rating description')}\n"

            total_points += points
            result += "\n"

        result += f"Total Rubric Points: {total_points}"

        return result

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_rubric_assessment(course_identifier: str | int,
                                             assignment_id: str | int,
                                             user_id: str | int) -> str:
        """Get rubric assessment scores for a specific submission.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Canvas assignment ID
            user_id: Canvas user ID of the student
        """
        course_id = await get_course_id(course_identifier)
        assignment_id_str = str(assignment_id)
        user_id_str = str(user_id)

        # Get submission with rubric assessment
        response = await make_canvas_request(
            "get",
            f"/courses/{course_id}/assignments/{assignment_id_str}/submissions/{user_id_str}",
            params={"include[]": ["rubric_assessment", "full_rubric_assessment"]}
        )

        if "error" in response:
            return f"Error fetching submission rubric assessment: {response['error']}"

        # Anonymization happens at the client layer (core/client.py) per
        # ENABLE_DATA_ANONYMIZATION (#179)

        # Check if submission has rubric assessment
        rubric_assessment = response.get("rubric_assessment")

        if not rubric_assessment:
            # Get user and assignment names for better error message
            assignment_response = await make_canvas_request(
                "get", f"/courses/{course_id}/assignments/{assignment_id_str}"
            )
            assignment_name = assignment_response.get("name", "Unknown Assignment") if "error" not in assignment_response else "Unknown Assignment"

            course_display = await get_course_code(course_id) or course_identifier
            return (
                f"No rubric assessment found for user {user_id} on assignment "
                f"{fence_untrusted_inline(assignment_name, 'assignment name')} "
                f"in course {course_display}."
            )

        # Get assignment details for context
        assignment_response = await make_canvas_request(
            "get", f"/courses/{course_id}/assignments/{assignment_id_str}",
            params={"include[]": ["rubric"]}
        )

        assignment_name = assignment_response.get("name", "Unknown Assignment") if "error" not in assignment_response else "Unknown Assignment"
        rubric_data = assignment_response.get("rubric", []) if "error" not in assignment_response else []

        # Format rubric assessment
        course_display = await get_course_code(course_id) or course_identifier

        result = (
            f"Rubric Assessment for User {user_id} on "
            f"{fence_untrusted_inline(assignment_name, 'assignment name')} "
            f"in Course {course_display}:\n\n"
        )

        # Submission details
        submitted_at = format_date(response.get("submitted_at"))
        graded_at = format_date(response.get("graded_at"))
        score = response.get("score", "Not graded")

        result += "Submission Details:\n"
        result += f"  Submitted: {submitted_at}\n"
        result += f"  Graded: {graded_at}\n"
        result += f"  Score: {score}\n\n"

        # Rubric assessment details
        result += "Rubric Assessment:\n"
        result += "=" * 30 + "\n"

        total_rubric_points = 0

        for criterion_id, assessment in rubric_assessment.items():
            # Find criterion details from rubric data
            criterion_info = None
            for criterion in rubric_data:
                if str(criterion.get("id")) == str(criterion_id):
                    criterion_info = criterion
                    break

            criterion_description = criterion_info.get("description", f"Criterion {criterion_id}") if criterion_info else f"Criterion {criterion_id}"
            points = assessment.get("points", 0)
            comments = assessment.get("comments", "")
            rating_id = assessment.get("rating_id")

            # Criterion/rating descriptions (author) and comments (grader/peer)
            # are author-controlled (issue 239).
            result += f"\n{fence_untrusted_inline(criterion_description, 'rubric criterion description')}:\n"
            result += f"  Points Awarded: {points}\n"

            if rating_id and criterion_info:
                # Find the rating description
                for rating in criterion_info.get("ratings", []):
                    if str(rating.get("id")) == str(rating_id):
                        result += f"  Rating: {fence_untrusted_inline(rating.get('description', 'N/A'), 'rubric rating description')} ({rating.get('points', 0)} pts)\n"
                        break

            if comments:
                result += f"  Comments: {fence_untrusted_inline(comments, 'rubric assessment comment')}\n"

            total_rubric_points += points

        result += f"\nTotal Rubric Points: {total_rubric_points}"

        return result

