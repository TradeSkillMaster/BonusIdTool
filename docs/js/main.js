import { parseItemLink } from './link-parser.js';
import { Calculator } from './calculator.js';
import { formatSteps } from './formatter.js';

let calculator = null;

const EXAMPLES = [
    { label: 'Scaled item (Midnight)', link: '|cnIQ3:|Hitem:258912::::::::83:103::134:3:13613:6652:13578:2:9:90:28:4281::::|h[Tarnished Dawnlit Spellbinder\'s Robe]|h|r' },
    { label: 'TWW dungeon drop', link: '|cnIQ3:|Hitem:244501::::::::80:262::136:5:12267:6652:10844:3178:10254:1:28:3008:::::|h[Worn Shadowguard Captain\'s Breastplate]|h|r' },
];

async function init() {
    const buildInfo = document.getElementById('build-info');
    const input = document.getElementById('link-input');
    const calcBtn = document.getElementById('calc-btn');
    const errorDiv = document.getElementById('parse-error');
    const resultSection = document.getElementById('result-section');
    const resultLevel = document.getElementById('result-level');
    const stepsContainer = document.getElementById('steps-container');
    const examplesList = document.getElementById('examples-list');
    const parsedInfo = document.getElementById('parsed-info');

    // Load data
    buildInfo.textContent = 'Loading data...';
    try {
        const resp = await fetch('data/addon_data.json');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        buildInfo.textContent = `Build: ${data.build || 'unknown'}`;
        calculator = new Calculator(data);
    } catch (e) {
        buildInfo.textContent = `Failed to load data: ${e.message}`;
        calcBtn.disabled = true;
        return;
    }

    // Populate examples
    for (const ex of EXAMPLES) {
        const li = document.createElement('li');
        const a = document.createElement('a');
        a.href = '#';
        a.textContent = ex.label;
        a.addEventListener('click', (e) => {
            e.preventDefault();
            input.value = ex.link;
            doCalculate();
        });
        li.appendChild(a);
        examplesList.appendChild(li);
    }

    // Wire events
    calcBtn.addEventListener('click', doCalculate);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') doCalculate();
    });

    // Load from URL hash on startup
    const hashLink = decodeHashLink();
    if (hashLink) {
        input.value = hashLink;
        doCalculate();
    }

    // Re-calculate when navigating back/forward
    window.addEventListener('hashchange', () => {
        const link = decodeHashLink();
        if (link && link !== input.value) {
            input.value = link;
            doCalculate();
        }
    });

    function doCalculate() {
        errorDiv.classList.add('hidden');
        resultSection.classList.add('hidden');

        const parsed = parseItemLink(input.value);
        if (!parsed) {
            errorDiv.textContent = 'Could not parse item link. Paste a WoW item link or itemstring (e.g., |Hitem:258912:...).';
            errorDiv.classList.remove('hidden');
            return;
        }

        const steps = [];
        const stepCalc = createTracingCalculator(calculator, steps);
        const itemLevel = stepCalc.calculate(parsed.itemId, parsed.bonusIds, parsed.dropLevel, parsed.contentTuningId);

        resultLevel.textContent = itemLevel;
        let infoHtml = infoField('Item', parsed.itemId);
        infoHtml += infoField('Bonuses', parsed.bonusIds.join(', ') || 'none');
        if (parsed.dropLevel) infoHtml += infoField('Drop Level', parsed.dropLevel);
        if (parsed.contentTuningId) infoHtml += infoField('Content Tuning', parsed.contentTuningId);
        parsedInfo.innerHTML = infoHtml;
        stepsContainer.innerHTML = formatSteps(steps);
        resultSection.classList.remove('hidden');

        // Update URL hash (without triggering hashchange)
        const encoded = encodeURIComponent(input.value);
        const newHash = '#link=' + encoded;
        if (window.location.hash !== newHash) {
            history.pushState(null, '', newHash);
        }
    }
}

function infoField(label, value) {
    return `<span class="info-label">${label}:</span> <span class="info-value">${value}</span><br>`;
}

function decodeHashLink() {
    const hash = window.location.hash;
    if (!hash.startsWith('#link=')) return null;
    try {
        return decodeURIComponent(hash.substring(6));
    } catch {
        return null;
    }
}

/**
 * Create a Calculator that records steps into the given array.
 * We do this by creating a new Calculator from the same data but with onStep set.
 */
function createTracingCalculator(baseCalc, stepsArray) {
    // Copy internal state from baseCalc to a new object with onStep
    const traced = Object.create(Calculator.prototype);
    traced._bonuses = baseCalc._bonuses;
    traced._curves = baseCalc._curves;
    traced._squishCurveIndex = baseCalc._squishCurveIndex;
    traced._squishMax = baseCalc._squishMax;
    traced._contentTuning = baseCalc._contentTuning;
    traced._itemRangeStarts = baseCalc._itemRangeStarts;
    traced._itemRangeLevels = baseCalc._itemRangeLevels;
    traced._midnightItems = baseCalc._midnightItems;
    traced._treeBonusLists = baseCalc._treeBonusLists;
    traced._itemTreeBonuses = baseCalc._itemTreeBonuses;
    traced._onStep = (step) => stepsArray.push(step);
    return traced;
}

init();
