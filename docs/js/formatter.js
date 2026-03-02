/**
 * Format calculation steps into HTML.
 * @param {object[]} steps - Step objects from Calculator's onStep callback
 * @returns {string} HTML string
 */
export function formatSteps(steps) {
    const items = [];
    for (const step of steps) {
        const html = formatStep(step);
        if (html) items.push(html);
    }
    return `<ol class="steps">${items.join('')}</ol>`;
}

function formatStep(step) {
    switch (step.type) {
        case 'base_level':
            return li('info',
                `<strong>Look up base item level</strong>` +
                `<br>Item ${step.itemId} has a base item level of ${val(step.itemLevel)} from the game's item database.` +
                (step.hasMidnight
                    ? `<br>This item is from the <span class="tag midnight">Midnight</span> era, so its item level is already in the post-squish scale.`
                    : `<br>This item predates the Midnight expansion. Its item level will be squished at the end unless a scaling bonus overrides it.`)
            );

        case 'resolve_bonus_ids': {
            let html = `<strong>Process bonus IDs</strong>`;
            if (!arraysEqual(step.original, step.resolved)) {
                html += `<br>Input: ${step.original.map(id => bonusTag(id)).join(', ')}`;
                html += `<br>After resolving redirects and sorting by priority: ${step.resolved.map(id => bonusTag(id)).join(', ')}`;
            } else {
                html += `<br>Bonus IDs: ${step.resolved.map(id => bonusTag(id)).join(', ')}`;
                if (step.resolved.length === 0) html += ' (none)';
            }
            if (step.details && step.details.length > 0) {
                const active = step.details.filter(d => d.status === 'active');
                const skipped = step.details.filter(d => d.status === 'no_data');
                if (active.length > 0) {
                    html += `<br>Affects item level: ${active.map(d => bonusTag(d.id) + ' <span class="detail">(' + formatOp(d.op) + ')</span>').join(', ')}`;
                }
                if (skipped.length > 0) {
                    html += `<br>Skipped: ${skipped.map(d => bonusTag(d.id)).join(', ')} <span class="detail">&mdash; no item level effect (may affect stats, quality, appearance, or other properties)</span>`;
                }
            }
            return li('info', html);
        }

        case 'apply_legacy_add':
            return li('add',
                `<strong>Flat item level bonus</strong> from bonus ${bonusTag(step.bonus)}` +
                `<br>This bonus adds a fixed ${signed(step.amount)} to the item level.` +
                `<br>${transition(step.before, step.after)}`
            );

        case 'apply_add':
            return li('add',
                `<strong>Item level adjustment</strong> from bonus ${bonusTag(step.bonus)}` +
                `<br>Adds ${signed(step.amount)} to the item level` +
                (step.bonus.midnight === 'force' ? ' (this is a crafting quality or Midnight-era adjustment).' : '.') +
                `<br>${transition(step.before, step.after)}`
            );

        case 'midnight_force_squish':
            return li('squish',
                `<strong>Midnight squish triggered</strong> by bonus ${bonusTag(step.bonus)}` +
                `<br>This bonus (e.g., crafting quality) forces the item into the Midnight-era scale. ` +
                `The current item level is converted from the old scale to the new compressed scale using the squish curve.` +
                `<br>Squish: ${val(step.before)} &rarr; ${val(step.after)}`
            );

        case 'apply_set':
            return li('set',
                `<strong>Set item level</strong> from bonus ${bonusTag(step.bonus)}` +
                `<br>This bonus directly sets the item level to a fixed value of ${val(step.after)}, replacing the previous level entirely.` +
                `<br>${transition(step.before, step.after)}`
            );

        case 'apply_scale': {
            let html = `<strong>Scaling curve lookup</strong> from bonus ${bonusTag(step.bonus)}`;

            // Explain drop level source
            html += `<br>`;
            if (step.dropLevelSource === 'fixed') {
                html += `The bonus specifies a fixed drop level of ${val(step.dropLevel)} (this item always scales as if it dropped at this level).`;
            } else if (step.dropLevelSource === 'modifier') {
                html += `The drop level is ${val(step.dropLevel)}, taken from the item link's drop level modifier (type 9). This is the level at which the item dropped for the player.`;
            } else {
                html += `No drop level was specified, so using the default of ${val(step.dropLevel)}.`;
            }

            // Explain content tuning
            if (step.contentTuning) {
                const ct = step.contentTuning;
                if (ct.before === ct.after) {
                    html += `<br><span class="detail">Content tuning ${ct.contentTuningId} (${formatCtOp(ct.op)}) was checked but didn't change the drop level (${ct.before} is already within bounds).</span>`;
                } else {
                    html += `<br>Content tuning ${ct.contentTuningId} adjusts the drop level: ${formatCtOp(ct.op)} clamped ${val(ct.before)} to ${val(ct.after)}.`;
                }
            }
            if (step.ctSkipReason) {
                html += `<br><span class="detail">Content tuning was skipped: ${step.ctSkipReason}.</span>`;
            }

            // Explain curve lookup with nearby points
            html += `<br>The scaling curve maps drop level to item level. `;
            if (step.curveContext) {
                html += formatCurveContext(step.curveContext, step.dropLevel, step.curveResult);
            } else {
                html += `At drop level ${val(step.dropLevel)}, the curve returns ${val(step.curveResult)}.`;
            }
            if (step.offset) {
                html += `<br>A fixed offset of ${signed(step.offset)} is added: ${step.curveResult} ${signed(step.offset)} = ${val(step.curveResult + step.offset)}.`;
            }
            html += `<br>${transition(step.before, step.after)}`;
            return li('scale', html);
        }

        case 'extra_amount':
            return li('add',
                `<strong>Additional adjustment</strong> ${signed(step.amount)} from bonus ${bonusTag(step.bonus)}` +
                `<br>This bonus includes a secondary item level modifier on top of its primary effect.` +
                `<br>${transition(step.before, step.after)}`
            );

        case 'midnight_set':
            return li('midnight',
                `<strong>Midnight scaling flag set</strong> by bonus ${bonusTag(step.bonus)}` +
                `<br><span class="detail">The item is now in the Midnight-era scale. The final squish step will be skipped since the item level is already in the compressed scale.</span>`
            );

        case 'midnight_squish':
            return li('squish',
                `<strong>Apply Midnight squish</strong> after bonus ${bonusTag(step.bonus)}` +
                `<br>This bonus produces a pre-squish item level that needs to be converted to the Midnight-era compressed scale.` +
                `<br>Squish curve: ${val(step.before)} &rarr; ${val(step.after)}`
            );

        case 'final_clamp':
            return li('info',
                `<strong>Minimum clamp</strong>` +
                `<br>Item level cannot go below 1.` +
                `<br>${transition(step.before, step.after)}`
            );

        case 'final_squish':
            return li('squish',
                `<strong>Final Midnight squish</strong>` +
                `<br>This item was not flagged as Midnight-era by any bonus, so its item level is still in the old pre-squish scale. ` +
                `The squish curve compresses it into the Midnight-era range.` +
                `<br>Squish: ${val(step.before)} &rarr; ${val(step.after)}`
            );

        case 'result':
            return null; // Handled separately in main.js

        default:
            return null;
    }
}

function formatCurveContext(ctx, dropLevel, result) {
    let html = '';
    if (ctx.exact) {
        html += `Drop level ${val(dropLevel)} is an exact match in the curve, returning ${val(result)}.`;
    } else {
        // Find the two bracketing points
        const below = ctx.nearby.filter(([x]) => x < dropLevel);
        const above = ctx.nearby.filter(([x]) => x > dropLevel);
        if (below.length > 0 && above.length > 0) {
            const lo = below[below.length - 1];
            const hi = above[0];
            html += `Drop level ${val(dropLevel)} falls between curve points ` +
                `${val(lo[0])}&rarr;${val(Math.floor(lo[1] + 0.5))} and ` +
                `${val(hi[0])}&rarr;${val(Math.floor(hi[1] + 0.5))}. ` +
                `Linear interpolation gives ${val(result)}.`;
        } else {
            html += `At drop level ${val(dropLevel)}, the curve returns ${val(result)}.`;
        }
    }
    // Show nearby points as a mini table
    html += `<div class="curve-table"><table>`;
    html += `<tr><th>Level</th>`;
    for (const [x] of ctx.nearby) {
        const isMatch = x === dropLevel;
        html += `<td${isMatch ? ' class="curve-match"' : ''}>${x}</td>`;
    }
    // Insert the interpolated value if not exact
    html += `</tr><tr><th>iLvl</th>`;
    for (const [x, y] of ctx.nearby) {
        const isMatch = x === dropLevel;
        html += `<td${isMatch ? ' class="curve-match"' : ''}>${Math.floor(y + 0.5)}</td>`;
    }
    html += `</tr></table></div>`;
    return html;
}

function formatOp(op) {
    if (op === 'scale') return 'scaling curve';
    if (op === 'set') return 'set level';
    if (op === 'add') return 'add to level';
    if (op === 'legacy_add') return 'flat bonus';
    return op;
}

function formatCtOp(op) {
    if (!op) return 'unknown';
    const name = op[0];
    if (name === 'cap') return `cap at ${op[1]}`;
    if (name === 'clamp') return `clamp to ${op[1]}\u2013${op[2]}`;
    if (name === 'const') return `fixed at ${op[1]}`;
    if (name === 'cap_add') return `cap at ${op[1]}, then add ${op[2]}`;
    if (name === 'cap_add_floor') return `cap at ${op[1]}, add ${op[2]}, floor ${op[3]}`;
    return name;
}

function li(className, content) {
    return `<li class="step step-${className}">${content}</li>`;
}

function val(n) {
    return `<span class="val">${n}</span>`;
}

function signed(n) {
    return n >= 0 ? `+${n}` : String(n);
}

function transition(before, after) {
    if (before === after) return `Item level: ${val(after)} <span class="detail">(unchanged)</span>`;
    return `Item level: <span class="old">${before}</span> &rarr; ${val(after)}`;
}

function bonusTag(bonusOrId) {
    const id = typeof bonusOrId === 'object' ? bonusOrId._bonusId : bonusOrId;
    return `<span class="bonus-id">${id}</span>`;
}

function arraysEqual(a, b) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) {
        if (a[i] !== b[i]) return false;
    }
    return true;
}
