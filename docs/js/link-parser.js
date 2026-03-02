const DROP_LEVEL_MODIFIER = 9;
const CONTENT_TUNING_MODIFIER = 28;

/**
 * Parse a WoW item link or itemstring into its components.
 * Handles:
 *   - Full colored links: |cnIQ3:|Hitem:258912:...|h[Name]|h|r
 *   - Raw Hitem links: |Hitem:258912:...|h[Name]|h|r
 *   - Bare itemstrings: item:258912:...
 * @param {string} input
 * @returns {{ itemId: number, bonusIds: number[], dropLevel: number, contentTuningId: number } | null}
 */
export function parseItemLink(input) {
    input = input.trim();

    // Find the item: or Hitem: anchor
    const anchorMatch = input.match(/\|?H?item:(\d+)/i);
    if (!anchorMatch) return null;

    // Get everything from "item:NNNNN:" onward, strip trailing |h text
    const startIdx = anchorMatch.index + anchorMatch[0].length;
    let rest = input.substring(startIdx);
    // Remove |h[...] suffix and anything after
    const hIdx = rest.indexOf('|h');
    if (hIdx !== -1) rest = rest.substring(0, hIdx);

    const itemId = parseInt(anchorMatch[1], 10);
    if (isNaN(itemId) || itemId <= 0) return null;

    // Split remaining colon-separated fields
    // The format after itemID is: :enchant:gem1:gem2:gem3:gem4:suffix:unique:level:spec:?:context:numBonus:bonus1:...:numMods:modType1:modVal1:...
    // We need to skip the 12 fixed fields after itemID to get to numBonusIDs
    const parts = rest.split(':');
    // parts[0] is empty (leading colon), then 11 fixed fields = indices 1..11
    // numBonusIDs is at index 12
    const FIXED_FIELDS = 12;

    if (parts.length <= FIXED_FIELDS) {
        return { itemId, bonusIds: [], dropLevel: 0, contentTuningId: 0 };
    }

    let pos = FIXED_FIELDS;
    const numBonuses = parseInt(parts[pos], 10) || 0;
    pos++;

    const bonusIds = [];
    for (let i = 0; i < numBonuses && pos < parts.length; i++, pos++) {
        const id = parseInt(parts[pos], 10);
        if (!isNaN(id)) bonusIds.push(id);
    }

    const numModifiers = parseInt(parts[pos], 10) || 0;
    pos++;

    let dropLevel = 0;
    let contentTuningId = 0;
    for (let i = 0; i < numModifiers && pos + 1 < parts.length; i++) {
        const modType = parseInt(parts[pos], 10);
        const modValue = parseInt(parts[pos + 1], 10);
        pos += 2;
        if (modType === DROP_LEVEL_MODIFIER) dropLevel = modValue;
        else if (modType === CONTENT_TUNING_MODIFIER) contentTuningId = modValue;
    }

    return { itemId, bonusIds, dropLevel, contentTuningId };
}
