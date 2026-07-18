"""Read and patch item quantities inside a multiplayer character blob.

A Playerdata blob is one zlib stream. Decompressed, inventory entries
follow a recognisable pattern (verified against live characters by
matching known stack sizes):

    u32 length | ASCII item id | FF FF FF FF | 00 00 00 00 | u16 quantity | ...

Only that u16 is touched, in place, so every offset in the file stays
valid - no other knowledge of the surrounding format is required. Spell
entries (MAGIC_*) share the string layout but not the meaning of the
counter, so they are reported but flagged non-editable, as is any id
without a known stackable prefix.
"""

import re
import zlib
from dataclasses import dataclass

MAX_QUANTITY = 0xFFFF
MAX_CLASS = 99  # weapon class; verified up to 6 in game, capped defensively
# Item-id prefixes whose stack-count field may be edited.
EDITABLE_PREFIXES = (
    'ING_', 'MUSHROOM_', 'POTION_', 'LOCKPICK', 'TRAP_', 'ART_ADD_',
)
# Equipment carries its stacking level ("Klasse", which scales damage/
# armour and the display name) in a u16 at +22 after the count field;
# the count field itself is always 1 for gear. Verified in game: setting
# the field to 6 shows "Klasse: 6" with scaled damage.
# (Note: 'ART_ADD_' does not match 'AR_' - different prefix.)
GEAR_PREFIXES = ('WP_', 'AR_')
_CLASS_FIELD_OFFSET = 22

_RE_ID = re.compile(rb'[A-Z][A-Z0-9_]{2,39}')
_SIG = b'\xff\xff\xff\xff\x00\x00\x00\x00'


class CharDataError(Exception):
    pass


@dataclass
class ItemStack:
    name: str
    quantity: int
    qty_offset: int     # offset of the u16 inside the decompressed blob
    editable: bool
    kind: str = 'count'  # 'count' (stack size) or 'class' (gear level)

    @property
    def max_value(self):
        return MAX_CLASS if self.kind == 'class' else MAX_QUANTITY


def decompress(blob):
    try:
        return bytearray(zlib.decompress(blob))
    except zlib.error as exc:
        raise CharDataError(f'Not a character blob: {exc}') from exc


def compress(raw):
    return zlib.compress(bytes(raw))


def parse_items(raw):
    """Find all item stacks in a decompressed character. Sorted by offset."""
    items = []
    pos = 0
    while True:
        pos = raw.find(_SIG, pos)
        if pos == -1:
            break
        sig_at = pos
        pos += 1
        # Walk backwards: the signature directly follows a length-prefixed
        # ASCII identifier.
        found = None
        for length in range(3, 41):
            start = sig_at - length
            if start < 4:
                break
            candidate = bytes(raw[start:sig_at])
            if not _RE_ID.fullmatch(candidate):
                continue
            prefixed = int.from_bytes(raw[start - 4:start], 'little')
            if prefixed == length:
                found = candidate.decode('ascii')
                break
        if found is None:
            continue
        qty_off = sig_at + len(_SIG)
        if qty_off + 2 > len(raw):
            continue
        if found.startswith(GEAR_PREFIXES):
            # gear: expose the class field instead of the constant count
            cls_off = qty_off + _CLASS_FIELD_OFFSET
            if cls_off + 2 > len(raw):
                continue
            value = int.from_bytes(raw[cls_off:cls_off + 2], 'little')
            items.append(ItemStack(found, value, cls_off, True,
                                   kind='class'))
            continue
        qty = int.from_bytes(raw[qty_off:qty_off + 2], 'little')
        editable = found.startswith(EDITABLE_PREFIXES)
        items.append(ItemStack(found, qty, qty_off, editable))
    return items


def set_quantity(raw, stack, quantity):
    """Patch one stack's quantity/class in place (raw is a bytearray)."""
    if not 0 <= quantity <= stack.max_value:
        raise CharDataError(f'Value out of range: {quantity}')
    current = int.from_bytes(raw[stack.qty_offset:stack.qty_offset + 2],
                             'little')
    if current != stack.quantity:
        raise CharDataError(
            f'Stale entry for {stack.name}: file changed since parsing')
    raw[stack.qty_offset:stack.qty_offset + 2] = quantity.to_bytes(2, 'little')
    stack.quantity = quantity


def _entry_start(raw, stack):
    """Offset of the entry's type byte (one before the name length)."""
    field_base = stack.qty_offset - \
        (_CLASS_FIELD_OFFSET if stack.kind == 'class' else 0)
    return field_base - len(_SIG) - len(stack.name) - 4 - 1


def _entry_bounds(raw, items, idx):
    """(start, end) of one entry: type byte up to the next entry's."""
    start = _entry_start(raw, items[idx])
    if idx + 1 < len(items):
        end = _entry_start(raw, items[idx + 1])
    else:
        end = len(raw)
    return start, end


_REF_TYPE = (2).to_bytes(4, 'little')


def _strip_socket_refs(entry):
    """Drop trailing 12-byte socket references (instance, type=2, zero).

    A cloned piece of gear must not point at another item's socketed
    stone; the game removes/adds these blocks itself when socketing.
    """
    while len(entry) >= 12 and entry[-8:-4] == _REF_TYPE \
            and entry[-4:] == b'\x00\x00\x00\x00':
        entry = entry[:-12]
    return entry


def _max_instance_id(raw, items):
    top = 0
    for it in items:
        base = it.qty_offset - \
            (_CLASS_FIELD_OFFSET if it.kind == 'class' else 0)
        top = max(top, int.from_bytes(raw[base + 2:base + 6], 'little'))
    return top


def add_gear(blob, template_name, new_name, new_class=1):
    """Clone a piece of gear under a new item id.

    The template's whole entry is copied (minus socket references), the
    item id, instance id and class are replaced, and the clone is spliced
    in directly after the template so it lands in the same container.
    Returns the recompressed blob.
    """
    if not new_name.startswith(GEAR_PREFIXES):
        raise CharDataError(f'{new_name} is not an equipment id')
    if not 1 <= new_class <= MAX_CLASS:
        raise CharDataError(f'Class out of range: {new_class}')
    raw = decompress(blob)
    items = parse_items(raw)
    template = None
    for idx, it in enumerate(items):
        if it.name == template_name and it.kind == 'class':
            template = idx
            break
    if template is None:
        raise CharDataError(f'No {template_name} found to clone from')
    if any(i.name == new_name for i in items):
        raise CharDataError(f'{new_name} already exists in this character')

    start, end = _entry_bounds(raw, items, template)
    entry = _strip_socket_refs(bytes(raw[start:end]))
    # swap the length-prefixed name
    old = template_name.encode('ascii')
    new = new_name.encode('ascii')
    head_len = 1 + 4 + len(old)  # type byte, name length, name
    entry = (entry[0:1] + len(new).to_bytes(4, 'little') + new
             + entry[head_len:])
    # fields sit after name + signature
    base = 1 + 4 + len(new) + len(_SIG)
    entry = bytearray(entry)
    entry[base + 2:base + 6] = \
        (_max_instance_id(raw, items) + 1).to_bytes(4, 'little')
    entry[base + _CLASS_FIELD_OFFSET:base + _CLASS_FIELD_OFFSET + 2] = \
        int(new_class).to_bytes(2, 'little')
    out = raw[:end] + entry + raw[end:]
    # sanity: the result must parse and contain the new entry
    check = {i.name: i for i in parse_items(out)}
    if new_name not in check or check[new_name].quantity != new_class:
        raise CharDataError('Clone verification failed')
    if len(parse_items(out)) != len(items) + 1:
        raise CharDataError('Clone corrupted neighbouring entries')
    return compress(out)


def edit_quantities(blob, changes):
    """Apply {qty_offset: new_quantity} to a compressed blob.

    Returns the recompressed blob. Offsets must come from parse_items()
    on this same blob.
    """
    raw = decompress(blob)
    stacks = {s.qty_offset: s for s in parse_items(raw)}
    for offset, quantity in changes.items():
        stack = stacks.get(offset)
        if stack is None:
            raise CharDataError(f'No item stack at offset {offset:#x}')
        if not stack.editable:
            raise CharDataError(f'{stack.name} is not editable')
        set_quantity(raw, stack, quantity)
    return compress(raw)
