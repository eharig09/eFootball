"""A pick-value-by-slot trade chart, for comparing draft capital in one unit.

The classic Jimmy Johnson chart is decades out of date -- real trade behavior
has drifted well below it, especially in the top ten -- so this uses a modern
points curve instead. It describes value *by slot*, not by team, so it stays
usable every year: only which team currently holds a slot changes.

This is a reference curve for comparing picks, not a market-clearing price;
actual trades vary with team-specific need, cap timing, and how many picks
change hands at once.
"""

from __future__ import annotations

#: Points per overall pick, 1-224. The tail beyond pick 224 (late round-7
#: compensatory picks) is not in the source chart and falls back to the
#: lowest tabulated value.
PICK_VALUE: dict[int, int] = {
    1: 3000, 2: 2600, 3: 2200, 4: 1800, 5: 1700, 6: 1600, 7: 1500, 8: 1400, 9: 1350, 10: 1300,
    11: 1250, 12: 1200, 13: 1150, 14: 1100, 15: 1050, 16: 1000, 17: 950, 18: 900, 19: 875, 20: 850,
    21: 800, 22: 780, 23: 760, 24: 740, 25: 720, 26: 700, 27: 680, 28: 660, 29: 640, 30: 620,
    31: 600, 32: 590, 33: 580, 34: 560, 35: 550, 36: 540, 37: 530, 38: 520, 39: 510, 40: 500,
    41: 490, 42: 480, 43: 470, 44: 460, 45: 450, 46: 440, 47: 430, 48: 420, 49: 410, 50: 400,
    51: 390, 52: 380, 53: 370, 54: 360, 55: 350, 56: 340, 57: 330, 58: 320, 59: 310, 60: 300,
    61: 292, 62: 284, 63: 276, 64: 270, 65: 265, 66: 260, 67: 255, 68: 250, 69: 245, 70: 240,
    71: 235, 72: 230, 73: 225, 74: 220, 75: 215, 76: 210, 77: 205, 78: 200, 79: 195, 80: 190,
    81: 185, 82: 180, 83: 175, 84: 170, 85: 165, 86: 160, 87: 155, 88: 150, 89: 145, 90: 140,
    91: 136, 92: 132, 93: 128, 94: 124, 95: 120, 96: 116, 97: 112, 98: 108, 99: 104, 100: 100,
    101: 96, 102: 92, 103: 88, 104: 86, 105: 84, 106: 82, 107: 80, 108: 78, 109: 76, 110: 74,
    111: 72, 112: 70, 113: 68, 114: 66, 115: 64, 116: 62, 117: 60, 118: 58, 119: 56, 120: 54,
    121: 52, 122: 50, 123: 49, 124: 48, 125: 47, 126: 46, 127: 45, 128: 44, 129: 43, 130: 42,
    131: 41, 132: 40, 133: 40, 134: 39, 135: 39, 136: 38, 137: 38, 138: 37, 139: 37, 140: 36,
    141: 36, 142: 35, 143: 35, 144: 34, 145: 34, 146: 33, 147: 33, 148: 32, 149: 32, 150: 31,
    151: 31, 152: 31, 153: 30, 154: 30, 155: 29, 156: 29, 157: 29, 158: 28, 159: 28, 160: 27,
    161: 27, 162: 27, 163: 26, 164: 26, 165: 25, 166: 25, 167: 25, 168: 24, 169: 24, 170: 23,
    171: 23, 172: 23, 173: 22, 174: 22, 175: 21, 176: 21, 177: 21, 178: 20, 179: 20, 180: 19,
    181: 19, 182: 19, 183: 18, 184: 18, 185: 17, 186: 17, 187: 17, 188: 16, 189: 16, 190: 15,
    191: 15, 192: 15, 193: 14, 194: 14, 195: 13, 196: 13, 197: 13, 198: 12, 199: 12, 200: 11,
    201: 11, 202: 11, 203: 10, 204: 10, 205: 9, 206: 9, 207: 9, 208: 8, 209: 8, 210: 7,
    211: 7, 212: 7, 213: 6, 214: 6, 215: 5, 216: 5, 217: 5, 218: 4, 219: 4, 220: 3,
    221: 3, 222: 3, 223: 2, 224: 2,
}

_FLOOR_VALUE = min(PICK_VALUE.values())
_MAX_TABULATED_PICK = max(PICK_VALUE)


def pick_value(pick: int) -> int | None:
    """Chart value for one overall pick, or `None` for a pick number below 1."""
    if pick < 1:
        return None
    if pick > _MAX_TABULATED_PICK:
        return _FLOOR_VALUE
    return PICK_VALUE[pick]


def combined_value(picks: list[int]) -> int:
    """Total chart value of a package of picks, for comparing both sides of a trade."""
    return sum(pick_value(pick) or 0 for pick in picks)
