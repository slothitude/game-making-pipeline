class_name Weapon
extends RefCounted
## The plane's auto-fire weapon (milestone 2). RefCounted, node-hosted by the
## plane (PlayerPlane.weapon); bullets go into an injected BulletPool.
## 4 tiers: 1 single, 2 twin, 3 twin + angled pair, 4 triple stream.
## Kills fill the charge meter; a full meter lets the Super Shot loose for
## Feel.SUPER_SHOT_DURATION seconds of beam while normal fire is suspended.

signal super_started
signal super_ended

var tier := Feel.WEAPON_TIER_MIN
var kills_charged := 0
var super_time_left := 0.0

var pool: BulletPool = null      # injected (player bullet pool)
var fire_cooldown := 0.0
var volleys_fired := 0
var shots_spawned := 0
var supers_fired := 0


func super_ready() -> bool:
	return kills_charged >= Feel.SUPER_SHOT_CHARGE_KILLS and not super_active()


func super_active() -> bool:
	return super_time_left > 0.0


## One enemy died -> charge the meter (saturates at full).
func register_kill() -> void:
	kills_charged = mini(kills_charged + 1, Feel.SUPER_SHOT_CHARGE_KILLS)


## Spend the meter. Returns false if the super is not ready.
func release_super() -> bool:
	if not super_ready():
		return false
	kills_charged = 0
	super_time_left = Feel.SUPER_SHOT_DURATION
	supers_fired += 1
	super_started.emit()
	return true


## One frame of the weapon. origin = muzzle world position.
## Returns bullets spawned this frame (0 while the super suspends normal fire).
func step(delta: float, origin: Vector2) -> int:
	var spawned := 0
	if super_active():
		super_time_left -= delta
		if super_time_left <= 0.0:
			super_time_left = 0.0
			super_ended.emit()
		if Feel.SUPER_SUSPENDS_NORMAL_FIRE:
			return 0
	fire_cooldown -= delta
	if fire_cooldown <= 0.0:
		fire_cooldown += 1.0 / Feel.FIRE_RATE
		spawned = fire_now(origin)
	return spawned


## Fire one volley of the current tier right now (also the test seam).
func fire_now(origin: Vector2) -> int:
	if pool == null:
		return 0
	var count := 0
	for shot in volley_plan(tier):
		var dir := Vector2(sin(shot.y), -cos(shot.y))  # shot.y = angle off straight up
		if pool.spawn(origin + Vector2(shot.x, 0.0), dir * Feel.BULLET_SPEED,
				Feel.PLAYER_BULLET_DAMAGE, Feel.PLAYER_BULLET_RADIUS, true) != null:
			count += 1
	volleys_fired += 1
	shots_spawned += count
	return count


## Pure pattern description: array of Vector2(muzzle_x_offset, angle_rad).
## Tier 1 single / 2 twin / 3 twin + angled pair / 4 triple stream.
func volley_plan(for_tier: int) -> Array:
	match for_tier:
		2:
			return [
				Vector2(-Feel.TIER_TWIN_OFFSET_PX, 0.0),
				Vector2(Feel.TIER_TWIN_OFFSET_PX, 0.0),
			]
		3:
			return [
				Vector2(-Feel.TIER_TWIN_OFFSET_PX, 0.0),
				Vector2(Feel.TIER_TWIN_OFFSET_PX, 0.0),
				Vector2(-Feel.TIER_ANGLED_OFFSET_PX, -Feel.TIER_ANGLED_RAD),
				Vector2(Feel.TIER_ANGLED_OFFSET_PX, Feel.TIER_ANGLED_RAD),
			]
		4:
			return [
				Vector2(-Feel.TIER_TRIPLE_OFFSET_PX, 0.0),
				Vector2(0.0, 0.0),
				Vector2(Feel.TIER_TRIPLE_OFFSET_PX, 0.0),
			]
		_:
			return [Vector2(0.0, 0.0)]
