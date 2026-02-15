local BonusIdAlgorithm = {}
select(2, ...).BonusIdAlgorithm = BonusIdAlgorithm
local BonusId = nil
local private = {
	squishMax = -math.huge,
	contentTuning = nil,
	allPartsTemp = {},
	partsTemp = {},
	bonusIdsTemp = {},
	collectedTemp = {},
}
local OP_GROUP = { scale = "level", set = "level", add = "add" }
local DEFAULT_DROP_LEVEL = 80



-- ============================================================================
-- Module Functions
-- ============================================================================

function BonusIdAlgorithm.Initialize(data)
	BonusId = data
	for k in pairs(BonusId.squishCurve) do
		private.squishMax = max(private.squishMax, k)
	end
end

function BonusIdAlgorithm.ProcessItem(link, itemLevel, hasMidnightScaling)
	assert(not next(private.collectedTemp) and not next(private.bonusIdsTemp))
	local linkDropLevel, linkContentTuningId = private.ParseLink(link, private.bonusIdsTemp)
	private.ResolveBonusIds(private.bonusIdsTemp)

	-- Collect indirect first, then direct (direct overrides via dedup)
	for _, bonusId in ipairs(private.bonusIdsTemp) do
		local bonus = BonusId.bonuses[bonusId]
		if bonus and not bonus.redirect then
			if bonus.indirect then
				private.CollectBonus(bonus)
			end
			hasMidnightScaling = hasMidnightScaling or bonus.midnight == "set"
		end
	end
	for _, bonusId in ipairs(private.bonusIdsTemp) do
		local bonus = BonusId.bonuses[bonusId]
		if bonus and not bonus.redirect and not bonus.indirect then
			private.CollectBonus(bonus)
		end
	end
	wipe(private.bonusIdsTemp)

	if #private.collectedTemp == 0 then
		if not hasMidnightScaling then
			itemLevel = private.GetSquishValue(itemLevel)
		end
		return itemLevel
	end

	for _, bonus in ipairs(private.collectedTemp) do
		local midnightOp = bonus.midnight
		local op = bonus.op
		if op == "legacyAdd" then
			itemLevel = itemLevel + bonus.amount
		elseif op == "add" then
			if midnightOp == "force" and not hasMidnightScaling then
				hasMidnightScaling = true
				itemLevel = private.GetSquishValue(itemLevel)
			end
			itemLevel = itemLevel + bonus.amount
		elseif op == "set" then
			itemLevel = bonus.itemLevel
		elseif op == "scale" then
			local dropLevel = bonus.defaultLevel or linkDropLevel or DEFAULT_DROP_LEVEL
			if bonus.contentTuningKey then
				local contentTuningId = linkContentTuningId or bonus.contentTuningId
				if not bonus.contentTuningDefaultOnly or not linkDropLevel then
					dropLevel = private.ApplyContentTuning(dropLevel, contentTuningId, bonus.contentTuningKey)
				end
			end
			itemLevel = private.GetCurveValue(bonus.curveId, dropLevel) + (bonus.offset or 0)
		else
			error("Unknown bonus op: "..tostring(op))
		end

		if bonus.extraAmount then
			itemLevel = itemLevel + bonus.extraAmount
		end

		-- Post-op midnight handling
		if midnightOp == "set" then
			hasMidnightScaling = true
		elseif midnightOp == "squish" and hasMidnightScaling then
			itemLevel = private.GetSquishValue(itemLevel)
		end
	end
	wipe(private.collectedTemp)

	itemLevel = max(itemLevel, 1)
	if not hasMidnightScaling then
		itemLevel = private.GetSquishValue(itemLevel)
	end
	return itemLevel
end



-- ============================================================================
-- Private Helper Functions
-- ============================================================================

function private.CollectBonus(bonus)
	local group = OP_GROUP[bonus.op]
	if not group then
		tinsert(private.collectedTemp, bonus)
		return
	end

	local prevIndex = private.collectedTemp[group]
	if not prevIndex then
		tinsert(private.collectedTemp, bonus)
		private.collectedTemp[group] = #private.collectedTemp
		return
	end

	-- This new bonus overrides the previous one if it's higher priority (lower value)
	-- or just if it comes afterwards and there's no priority set
	local prevPriority = private.collectedTemp[prevIndex].priority
	local newPriority = bonus.priority
	if not prevPriority or not newPriority or newPriority <= prevPriority then
		private.collectedTemp[prevIndex] = bonus
	end
end

function private.GetSquishValue(value)
	if value > private.squishMax then
		return 1
	end
	return private.Interpolate(BonusId.squishCurve, value)
end

function private.GetCurveValue(curveId, value)
	return private.Interpolate(BonusId.curves[curveId + 1], value)
end

function private.ApplyContentTuning(dropLevel, ctId, ctKey)
	local contentTuning = BonusId.contentTuning[ctId]
	if not contentTuning then
		return dropLevel
	end
	local op = contentTuning[ctKey] or contentTuning.op
	if not op then
		return dropLevel
	end
	local name, value1, value2, value3 = unpack(op)
	if name == "cap" then
		return min(dropLevel, value1)
	elseif name == "clamp" then
		return min(max(dropLevel, value1), value2)
	elseif name == "const" then
		return value1
	elseif name == "capAdd" then
		return min(dropLevel, value1) + value2
	elseif name == "capAddFloor" then
		return max(min(dropLevel, value1) + value2, value3)
	end
	return dropLevel
end

function private.SortBonusIds(a, b)
	local bonusA = BonusId.bonuses[a]
	local bonusB = BonusId.bonuses[b]
	local priorityA = bonusA and bonusA.sortPriority or 0
	local priorityB = bonusB and bonusB.sortPriority or 0
	if priorityA ~= priorityB then
		return priorityA < priorityB
	end
	return a < b
end

function private.ResolveBonusIds(ids)
	for i, id in ipairs(ids) do
		local bonus = BonusId.bonuses[id]
		if bonus and bonus.redirect then
			ids[i] = bonus.redirect
		end
	end
	sort(ids, private.SortBonusIds)
	return ids
end

function private.ParseLink(link, bonusIdsTbl)
	-- Split on ':'
	wipe(private.allPartsTemp)
	for part in gmatch(link, "([^:]*):") do
		tinsert(private.allPartsTemp, part)
	end

	-- Variable-length section starts at index 15 (1-indexed)
	-- Filter out parts starting with "|h"
	wipe(private.partsTemp)
	for i = 15, #private.allPartsTemp do
		local p = private.allPartsTemp[i]
		if strsub(p, 1, 2) ~= "|h" then
			tinsert(private.partsTemp, p)
		end
	end

	local idx = 1

	local numBonusIds = 0
	if private.partsTemp[idx] and private.partsTemp[idx] ~= "" then
		numBonusIds = tonumber(private.partsTemp[idx])
	end
	idx = idx + 1

	for _ = 1, numBonusIds do
		tinsert(bonusIdsTbl, tonumber(private.partsTemp[idx]))
		idx = idx + 1
	end

	local numModifiers = 0
	if private.partsTemp[idx] and private.partsTemp[idx] ~= "" then
		numModifiers = tonumber(private.partsTemp[idx])
	end
	idx = idx + 1

	local dropLevel = nil
	local contentTuningId = nil
	for _ = 1, numModifiers do
		local modType = tonumber(private.partsTemp[idx])
		local modValue = tonumber(private.partsTemp[idx + 1])
		idx = idx + 2
		if modType == 9 and modValue ~= 0 then
			dropLevel = modValue
		elseif modType == 28 and modValue ~= 0 then
			contentTuningId = modValue
		end
	end

	return dropLevel, contentTuningId
end

function private.Interpolate(curve, value)
	local lowerBound, upperBound = -math.huge, math.huge
	for level, itemLevel in pairs(curve) do
		if level == value then
			return floor(itemLevel + 0.5)
		elseif level < value then
			lowerBound = max(lowerBound, level)
		else
			upperBound = min(upperBound, level)
		end
	end
	if lowerBound == -math.huge then
		return floor(curve[upperBound] + 0.5)
	elseif upperBound == math.huge then
		return floor(curve[lowerBound] + 0.5)
	end
	return floor(curve[lowerBound] + (value - lowerBound) / (upperBound - lowerBound) * (curve[upperBound] - curve[lowerBound]) + 0.5)
end
