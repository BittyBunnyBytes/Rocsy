-- {{NAME}}: bullet time for an Unreal Engine game (Rocsy REA recipe, UE4SS Lua).
-- T toggles the engine's global time dilation (the same thing as the "slomo" console command).
-- Change SLOW below; logs go to ue4ss/UE4SS.log and the UE4SS console.
local SLOW = 0.3
local active = false

local function statics()
    return StaticFindObject("/Script/Engine.Default__GameplayStatics")
end

local function setDilation(value)
    local pc = FindFirstOf("PlayerController")
    local gs = statics()
    if not pc or not pc:IsValid() or not gs or not gs:IsValid() then
        print("[{{NAME}}] no player controller yet\n")
        return
    end
    gs:SetGlobalTimeDilation(pc, value)
end

RegisterKeyBind(Key.T, function()
    ExecuteInGameThread(function()
        active = not active
        setDilation(active and SLOW or 1.0)
        print(string.format("[{{NAME}}] bullet time %s\n", active and "on" or "off"))
    end)
end)

print("[{{NAME}}] loaded: press T for bullet time\n")
