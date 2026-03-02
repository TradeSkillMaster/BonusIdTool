const OP_GROUP = { scale: 'S', set: 'S', add: 'Q' };
const TREE_BONUS_ID = 3524;

export class Calculator {
    /**
     * @param {object} data - Parsed addon_data.json
     * @param {{ onStep?: (step: object) => void }} [opts]
     */
    constructor(data, opts) {
        this._bonuses = data.bonuses;
        this._curves = data.curves;
        this._squishCurveIndex = data.squish_curve;
        this._squishMax = data.squish_max;
        this._contentTuning = Object.assign({}, data.content_tuning);
        this._itemRangeStarts = data.item_range_starts || [];
        this._itemRangeLevels = data.item_range_levels || [];
        this._midnightItems = new Set(data.midnight_items || []);
        this._treeBonusLists = data.tree_bonus_lists || [];
        this._itemTreeBonuses = data.item_tree_bonuses || {};
        this._onStep = (opts && opts.onStep) || null;

        // Expand content tuning remap
        const remap = data.content_tuning_remap || {};
        for (const [src, dst] of Object.entries(remap)) {
            const dstStr = String(dst);
            if (this._contentTuning[dstStr]) {
                this._contentTuning[String(src)] = this._contentTuning[dstStr];
            }
        }
    }

    _step(data) {
        if (this._onStep) this._onStep(data);
    }

    /**
     * Calculate item level from parsed item link components.
     * @param {number} itemId
     * @param {number[]} bonusIds
     * @param {number} dropLevel - Modifier type 9 value (0 if not present)
     * @param {number} contentTuningId - Modifier type 28 value (0 if not present)
     * @returns {number}
     */
    calculate(itemId, bonusIds, dropLevel, contentTuningId) {
        const [baseLevel, hasMidnight] = this._getItemInfo(itemId);
        const item = {
            itemId,
            itemLevel: baseLevel,
            hasMidnight,
            dropLevel,
            contentTuningId,
        };

        this._step({ type: 'base_level', itemId, itemLevel: baseLevel, hasMidnight });

        const resolvedBonusIds = this._getBonusIds(bonusIds, itemId);
        // Classify each resolved bonus ID for the explanation
        const bonusIdDetails = resolvedBonusIds.map(id => {
            const data = this._bonuses[String(id)];
            if (!data) return { id, status: 'no_data' };
            if (data.redirect != null) return { id, status: 'redirect', target: data.redirect };
            return { id, status: 'active', op: data.op, indirect: !!data.indirect };
        });
        this._step({ type: 'resolve_bonus_ids', original: bonusIds, resolved: resolvedBonusIds, details: bonusIdDetails });

        const bonuses = this._collectBonuses(resolvedBonusIds, item);

        if (bonuses.length === 0) {
            if (!item.hasMidnight) {
                const before = item.itemLevel;
                item.itemLevel = this._getSquishValue(item.itemLevel);
                this._step({ type: 'final_squish', before, after: item.itemLevel });
            }
            this._step({ type: 'result', itemLevel: item.itemLevel });
            return item.itemLevel;
        }

        for (const bonus of bonuses) {
            const op = bonus.op;
            if (op === 'legacy_add') {
                const before = item.itemLevel;
                item.itemLevel += bonus.amount;
                this._step({ type: 'apply_legacy_add', bonus, amount: bonus.amount, before, after: item.itemLevel });
            } else if (op === 'add') {
                if (bonus.midnight === 'force' && !item.hasMidnight) {
                    item.hasMidnight = true;
                    const before = item.itemLevel;
                    item.itemLevel = this._getSquishValue(item.itemLevel);
                    this._step({ type: 'midnight_force_squish', bonus, before, after: item.itemLevel });
                }
                const before = item.itemLevel;
                item.itemLevel += bonus.amount;
                this._step({ type: 'apply_add', bonus, amount: bonus.amount, before, after: item.itemLevel });
            } else if (op === 'set') {
                const before = item.itemLevel;
                item.itemLevel = bonus.item_level;
                this._step({ type: 'apply_set', bonus, before, after: item.itemLevel });
            } else if (op === 'scale') {
                let dl = bonus.default_level || dropLevel || 80;
                let dropLevelSource = bonus.default_level
                    ? 'fixed' : dropLevel ? 'modifier' : 'default';
                let ctApplied = null;
                let ctSkipReason = null;
                if (!bonus.default_level && bonus.content_tuning_key) {
                    const ct = contentTuningId || bonus.content_tuning_id;
                    if (ct && (!bonus.content_tuning_default_only || !dropLevel)) {
                        const dlBefore = dl;
                        const ctOp = this._getContentTuningOp(ct, bonus.content_tuning_key);
                        dl = this._applyContentTuning(dl, ct, bonus.content_tuning_key);
                        ctApplied = { contentTuningId: ct, key: bonus.content_tuning_key, op: ctOp, before: dlBefore, after: dl };
                    } else if (ct && bonus.content_tuning_default_only && dropLevel) {
                        ctSkipReason = 'player drop level overrides content tuning';
                    }
                }
                const curveResult = this._getCurveValue(bonus.curve_id, dl);
                const curveContext = this._onStep ? this._getCurveContext(bonus.curve_id, dl) : null;
                const offset = bonus.offset || 0;
                const before = item.itemLevel;
                item.itemLevel = curveResult + offset;
                this._step({
                    type: 'apply_scale', bonus, dropLevel: dl, dropLevelSource,
                    contentTuning: ctApplied, ctSkipReason,
                    curveResult, curveContext, offset, before, after: item.itemLevel,
                });
            }

            if (bonus.extra_amount != null) {
                const before = item.itemLevel;
                item.itemLevel += bonus.extra_amount;
                this._step({ type: 'extra_amount', bonus, amount: bonus.extra_amount, before, after: item.itemLevel });
            }

            const midnight = bonus.midnight;
            if (midnight === 'set') {
                item.hasMidnight = true;
                this._step({ type: 'midnight_set', bonus });
            } else if (midnight === 'squish' && item.hasMidnight) {
                const before = item.itemLevel;
                item.itemLevel = this._getSquishValue(item.itemLevel);
                this._step({ type: 'midnight_squish', bonus, before, after: item.itemLevel });
            }
        }

        const beforeClamp = item.itemLevel;
        item.itemLevel = Math.max(item.itemLevel, 1);
        if (beforeClamp !== item.itemLevel) {
            this._step({ type: 'final_clamp', before: beforeClamp, after: item.itemLevel });
        }

        if (!item.hasMidnight) {
            const before = item.itemLevel;
            item.itemLevel = this._getSquishValue(item.itemLevel);
            this._step({ type: 'final_squish', before, after: item.itemLevel });
        }

        this._step({ type: 'result', itemLevel: item.itemLevel });
        return item.itemLevel;
    }

    _getItemInfo(itemId) {
        const starts = this._itemRangeStarts;
        // bisect_right
        let lo = 0, hi = starts.length;
        while (lo < hi) {
            const mid = (lo + hi) >>> 1;
            if (starts[mid] <= itemId) lo = mid + 1;
            else hi = mid;
        }
        const idx = lo - 1;
        const baseLevel = idx >= 0 ? this._itemRangeLevels[idx] : 0;
        const hasMidnight = this._midnightItems.has(itemId);
        return [baseLevel, hasMidnight];
    }

    _getBonusIds(bonusIds, itemId) {
        const result = [];
        for (const id of bonusIds) {
            if (id === TREE_BONUS_ID) {
                const listIndex = this._itemTreeBonuses[String(itemId)];
                if (listIndex != null) {
                    result.push(...this._treeBonusLists[listIndex]);
                    continue;
                }
            }
            const data = this._bonuses[String(id)];
            if (data && data.redirect != null) {
                result.push(data.redirect);
            } else {
                result.push(id);
            }
        }
        const bonuses = this._bonuses;
        result.sort((a, b) => {
            const pa = (bonuses[String(a)] || {}).sort_priority || 0;
            const pb = (bonuses[String(b)] || {}).sort_priority || 0;
            return pa !== pb ? pa - pb : a - b;
        });
        return result;
    }

    _collectBonuses(bonusIds, item) {
        const bonuses = [];

        const collectBonus = (bonus) => {
            if (bonus.midnight === 'set') {
                item.hasMidnight = true;
            }
            const group = OP_GROUP[bonus.op];
            if (group == null) {
                bonuses.push(bonus);
                return;
            }
            const prevIndex = bonuses.findIndex(b => OP_GROUP[b.op] === group);
            if (prevIndex === -1) {
                bonuses.push(bonus);
            } else {
                const prevPrio = bonuses[prevIndex].priority;
                const newPrio = bonus.priority;
                if (prevPrio != null && newPrio != null) {
                    if (newPrio <= prevPrio) bonuses[prevIndex] = bonus;
                } else {
                    bonuses[prevIndex] = bonus;
                }
            }
        };

        const collectAll = (bonusId) => {
            const data = this._bonuses[String(bonusId)];
            if (!data || data.redirect != null) return;
            collectBonus(Object.assign({ _bonusId: bonusId }, data));
        };

        // Indirect first, then direct
        for (const id of bonusIds) {
            const data = this._bonuses[String(id)];
            if (data && data.indirect) collectAll(id);
        }
        for (const id of bonusIds) {
            const data = this._bonuses[String(id)];
            if (data && !data.indirect) collectAll(id);
        }

        return bonuses;
    }

    _getCurveValue(curveId, value) {
        const points = this._curves[curveId];
        return this._interpolate(points, value);
    }

    /**
     * Get nearby curve points around a value for display purposes.
     * Returns { exact, lower, upper, nearby } where nearby is a small
     * window of sorted [x, y] pairs around the lookup value.
     */
    _getCurveContext(curveId, value) {
        const curve = this._curves[curveId];
        const sorted = Object.entries(curve)
            .map(([k, v]) => [parseFloat(k), v])
            .sort((a, b) => a[0] - b[0]);

        const exactIdx = sorted.findIndex(([x]) => x === value);
        if (exactIdx !== -1) {
            const start = Math.max(0, exactIdx - 2);
            const end = Math.min(sorted.length, exactIdx + 3);
            return { exact: true, matchIdx: exactIdx - start, nearby: sorted.slice(start, end) };
        }

        // Find bracketing points
        let lowerIdx = -1, upperIdx = -1;
        for (let i = 0; i < sorted.length; i++) {
            if (sorted[i][0] < value) lowerIdx = i;
            if (sorted[i][0] > value && upperIdx === -1) upperIdx = i;
        }
        const centerIdx = lowerIdx >= 0 ? lowerIdx : (upperIdx >= 0 ? upperIdx : 0);
        const start = Math.max(0, centerIdx - 1);
        const end = Math.min(sorted.length, centerIdx + 4);
        return { exact: false, nearby: sorted.slice(start, end) };
    }

    _getSquishValue(value) {
        if (value > this._squishMax) return 1;
        return this._getCurveValue(this._squishCurveIndex, value);
    }

    _interpolate(curve, value) {
        let lowerX = -Infinity, lowerY = 0;
        let upperX = Infinity, upperY = 0;
        for (const [k, v] of Object.entries(curve)) {
            const level = parseFloat(k);
            if (level === value) return Math.floor(v + 0.5);
            if (level < value) {
                if (level > lowerX) { lowerX = level; lowerY = v; }
            } else {
                if (level < upperX) { upperX = level; upperY = v; }
            }
        }
        if (lowerX === -Infinity) return Math.floor(upperY + 0.5);
        if (upperX === Infinity) return Math.floor(lowerY + 0.5);
        const result = lowerY + (value - lowerX) / (upperX - lowerX) * (upperY - lowerY);
        return Math.floor(result + 0.5);
    }

    _getContentTuningOp(contentTuningId, ctKey) {
        const ct = this._contentTuning[String(contentTuningId)];
        if (!ct) return null;
        return ct[ctKey] || ct.op || null;
    }

    _applyContentTuning(dropLevel, contentTuningId, ctKey) {
        const ct = this._contentTuning[String(contentTuningId)];
        if (!ct) return dropLevel;
        const op = ct[ctKey] || ct.op;
        if (!op) return dropLevel;
        const name = op[0];
        if (name === 'cap') return Math.min(dropLevel, op[1]);
        if (name === 'clamp') return Math.min(Math.max(dropLevel, op[1]), op[2]);
        if (name === 'const') return op[1];
        if (name === 'cap_add') return Math.min(dropLevel, op[1]) + op[2];
        if (name === 'cap_add_floor') return Math.max(Math.min(dropLevel, op[1]) + op[2], op[3]);
        return dropLevel;
    }
}
