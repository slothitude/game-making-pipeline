class_name StyleTracker
extends RefCounted
## Style scoring stub (spec: scoring.style_bonus) — milestone B.
## Riding / throwing / burrow-kill variety accumulates style points;
## every STYLE_PER_LIFE points grants +1 life. Main hooks life_granted to lives.

signal changed(points: int)
signal life_granted(total_points: int)

var points: int = 0
var lives_granted: int = 0


func add(kind: String) -> int:
	var amount := _amount_for(kind)
	if amount <= 0:
		return points
	points += amount
	changed.emit(points)
	while points >= (lives_granted + 1) * Feel.STYLE_PER_LIFE:
		lives_granted += 1
		life_granted.emit(points)
	return points


func _amount_for(kind: String) -> int:
	match kind:
		"ride":
			return Feel.STYLE_RIDE
		"throw_hit":
			return Feel.STYLE_THROW_HIT
		"burrow_kill":
			return Feel.STYLE_BURROW_KILL
	return 0


func lives_for_points(p: int) -> int:
	return p / Feel.STYLE_PER_LIFE
