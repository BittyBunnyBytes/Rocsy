-- {{NAME}}: a spawn tool for an Unreal Engine game (Rocsy REA recipe, UE4SS Lua).
-- UE4SS Lua has no drawing API, so this is key driven (the agent can build a widget menu later):
--   F6 next class   F5 previous class   F7 spawn it 3 m in front of you   F8 undo last   F9 rescan
-- Classes come from classes.txt next to this mod (one path per line, e.g. /Game/Props/BP_Crate.BP_Crate_C);
-- if that is empty, every loaded Blueprint class whose name starts with BP_ is offered.
local MOD_DIR = debug.getinfo(1, "S").source:match("@?(.*)[/\\]Scripts[/\\]")
local classes, index, spawned = {}, 1, {}

local function readList()
    local list = {}
    local f = MOD_DIR and io.open(MOD_DIR .. "/classes.txt", "r")
    if f then
        for line in f:lines() do
            line = line:gsub("^%s+", ""):gsub("%s+$", "")
            if line ~= "" and line:sub(1, 1) ~= "#" then table.insert(list, line) end
        end
        f:close()
    end
    return list
end

local function rescan()
    classes = {}
    for _, path in ipairs(readList()) do
        local cls = StaticFindObject(path)
        if not (cls and cls:IsValid()) and LoadAsset then cls = LoadAsset(path) end
        if cls and cls:IsValid() then table.insert(classes, cls) end
    end
    if #classes == 0 then
        for _, cls in ipairs(FindAllOf("BlueprintGeneratedClass") or {}) do
            if cls:IsValid() and cls:GetFName():ToString():match("^BP_") then table.insert(classes, cls) end
        end
    end
    index = 1
    print(string.format("[{{NAME}}] %d spawnable classes\n", #classes))
end

local function current()
    local cls = classes[index]
    return cls and cls:IsValid() and cls or nil
end

local function announce()
    local cls = current()
    print(string.format("[{{NAME}}] %d/%d %s\n", index, #classes, cls and cls:GetFName():ToString() or "(none)"))
end

local function spawn()
    local cls = current()
    local pc = FindFirstOf("PlayerController")
    local gs = StaticFindObject("/Script/Engine.Default__GameplayStatics")
    if not (cls and pc and pc:IsValid() and gs and gs:IsValid()) then return end
    local pawn = pc.Pawn
    if not (pawn and pawn:IsValid()) then return end
    local loc, fwd = pawn:K2_GetActorLocation(), pawn:GetActorForwardVector()
    local transform = {
        Rotation = { X = 0.0, Y = 0.0, Z = 0.0, W = 1.0 },
        Translation = { X = loc.X + fwd.X * 300.0, Y = loc.Y + fwd.Y * 300.0, Z = loc.Z + 50.0 },
        Scale3D = { X = 1.0, Y = 1.0, Z = 1.0 },
    }
    -- 1 = AlwaysSpawn (ignore collisions)
    local actor = gs:BeginDeferredActorSpawnFromClass(pawn, cls, transform, 1, nil)
    if actor and actor:IsValid() then
        gs:FinishSpawningActor(actor, transform)
        table.insert(spawned, actor)
        print(string.format("[{{NAME}}] spawned %s\n", cls:GetFName():ToString()))
    end
end

RegisterKeyBind(Key.F9, function() ExecuteInGameThread(rescan) end)
RegisterKeyBind(Key.F6, function() if #classes > 0 then index = index % #classes + 1 end announce() end)
RegisterKeyBind(Key.F5, function() if #classes > 0 then index = (index - 2) % #classes + 1 end announce() end)
RegisterKeyBind(Key.F7, function() ExecuteInGameThread(spawn) end)
RegisterKeyBind(Key.F8, function()
    ExecuteInGameThread(function()
        local actor = table.remove(spawned)
        if actor and actor:IsValid() then actor:K2_DestroyActor() end
    end)
end)

print("[{{NAME}}] loaded: F9 scan, F5/F6 choose, F7 spawn, F8 undo\n")
