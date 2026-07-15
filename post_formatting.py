"""Safe, deliberately small formatter for carousel post descriptions.

Supported authoring syntax:
  **bold**, *italic*, lines beginning with "- " for bullets, and line breaks.
All author text is escaped before it is placed between the generated tags.
"""
from markupsafe import Markup, escape


def _find_closing_marker(value, marker, content_start):
    if content_start >= len(value) or value[content_start].isspace():
        return -1
    end = value.find(marker, content_start + 1)
    while end != -1:
        if end > content_start and not value[end - 1].isspace():
            return end
        end = value.find(marker, end + len(marker))
    return -1


def _render_inline(value):
    parts = ()
    index = 0
    while index < len(value):
        if value.startswith('***', index):
            end = _find_closing_marker(value, '***', index + 3)
            if end > index + 3:
                content = escape(value[index + 3:end])
                parts = (*parts, Markup('<strong><em>'), content,
                         Markup('</em></strong>'))
                index = end + 3
                continue
            parts = (*parts, escape('***'))
            index += 3
            continue
        if value.startswith('**', index):
            end = _find_closing_marker(value, '**', index + 2)
            if end > index + 2:
                content = escape(value[index + 2:end])
                parts = (*parts, Markup('<strong>'), content,
                         Markup('</strong>'))
                index = end + 2
                continue
            parts = (*parts, escape('**'))
            index += 2
            continue
        if value.startswith('*', index):
            end = _find_closing_marker(value, '*', index + 1)
            if end > index + 1:
                content = escape(value[index + 1:end])
                parts = (*parts, Markup('<em>'), content, Markup('</em>'))
                index = end + 1
                continue
            parts = (*parts, escape('*'))
            index += 1
            continue

        next_marker = value.find('*', index + 1)
        end = next_marker if next_marker != -1 else len(value)
        parts = (*parts, escape(value[index:end]))
        index = end

    return Markup('').join(parts)


def _flush_blocks(blocks, paragraph, bullets):
    updated = blocks
    if paragraph:
        updated = (*updated, ('paragraph', paragraph))
    if bullets:
        updated = (*updated, ('bullets', bullets))
    return updated


def _parse_blocks(value):
    blocks = ()
    paragraph = ()
    bullets = ()

    for line in value.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if not line.strip():
            blocks = _flush_blocks(blocks, paragraph, bullets)
            paragraph, bullets = (), ()
        elif line.lstrip().startswith('- '):
            if paragraph:
                blocks = _flush_blocks(blocks, paragraph, ())
                paragraph = ()
            bullets = (*bullets, line.lstrip()[2:])
        else:
            if bullets:
                blocks = _flush_blocks(blocks, (), bullets)
                bullets = ()
            paragraph = (*paragraph, line)

    return _flush_blocks(blocks, paragraph, bullets)


def render_post_description(value):
    """Return safe HTML for the supported formatting subset."""
    rendered = ()
    for block_type, lines in _parse_blocks(str(value or '')):
        if block_type == 'bullets':
            items = Markup('').join(
                Markup('<li>') + _render_inline(line) + Markup('</li>')
                for line in lines)
            rendered = (*rendered, Markup('<ul>') + items + Markup('</ul>'))
        else:
            content = Markup('<br>').join(
                _render_inline(line) for line in lines)
            rendered = (*rendered,
                        Markup('<p>') + content + Markup('</p>'))
    return Markup('').join(rendered)
