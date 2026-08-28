"""URL-template placeholder extraction and strict formatting."""

from string import Formatter


def get_template_placeholders(template: str) -> set[str]:
    return {
        field_name.split(".", 1)[0].split("[", 1)[0]
        for _, field_name, _, _ in Formatter().parse(template)
        if field_name
    }


def format_url_template(template: str, **values: str | None) -> str:
    placeholders = get_template_placeholders(template)
    missing_placeholders = [
        name for name in placeholders if name not in values or values[name] is None
    ]
    if missing_placeholders:
        missing_fields = ", ".join(sorted(missing_placeholders))
        raise ValueError(f"Missing format values for {missing_fields}: {template}")

    normalized_values = {}
    for name in placeholders:
        value = values[name]
        if isinstance(value, str):
            normalized_values[name] = value.strip()
        else:
            normalized_values[name] = value
    return template.format(**normalized_values)
