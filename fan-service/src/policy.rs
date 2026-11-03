use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Policy {
    pub curve_enabled: bool,
    pub curve_points: Vec<[u8; 2]>,
    pub curve_rise_delay: u16,
    pub curve_fall_delay: u16,
    pub cpu_fan_mode: String,
    pub cpu_fan_pwm: u8,
    pub gpu_fan_mode: String,
    pub gpu_fan_pwm: u8,
}

impl Policy {
    pub fn validate(&self) -> Result<(), String> {
        if !(2..=16).contains(&self.curve_points.len())
            || self.curve_points.iter().any(|p| p[0] > 110 || p[1] > 100)
            || self.curve_points.windows(2).any(|p| p[0][0] >= p[1][0])
        {
            return Err(
                "Curve points must have increasing temperatures and percentages from 0 to 100"
                    .into(),
            );
        }
        if self.curve_rise_delay > 120 || self.curve_fall_delay > 300 {
            return Err("Curve response delay is outside the supported range".into());
        }
        for (mode, percent) in [
            (&self.cpu_fan_mode, self.cpu_fan_pwm),
            (&self.gpu_fan_mode, self.gpu_fan_pwm),
        ] {
            if !["自动", "满速", "自定义"].contains(&mode.as_str()) || percent > 100 {
                return Err("Invalid manual fan setting".into());
            }
        }
        Ok(())
    }

    pub fn percent(&self, temperature: u8) -> u8 {
        let points = &self.curve_points;
        if temperature <= points[0][0] {
            return points[0][1];
        }
        for pair in points.windows(2) {
            let ([t0, p0], [t1, p1]) = (pair[0], pair[1]);
            if temperature <= t1 {
                let value = p0 as f64
                    + (temperature - t0) as f64 * (p1 as f64 - p0 as f64) / (t1 - t0) as f64;
                return value.clamp(0.0, 100.0) as u8;
            }
        }
        points.last().unwrap()[1]
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub enum Mode {
    Auto,
    Full,
    Custom(u8),
}

pub fn manual(mode: &str, percent: u8) -> Mode {
    match mode {
        "满速" => Mode::Full,
        "自定义" => Mode::Custom((percent as u16 * 3 / 2) as u8),
        _ => Mode::Auto,
    }
}

#[derive(Default)]
pub struct Controller {
    applied: Option<[Mode; 2]>,
    applied_temp: u8,
    applied_at: u64,
    pending: Option<(bool, u64)>,
}

impl Controller {
    pub fn reset(&mut self) {
        *self = Self::default();
    }

    pub fn plan(
        &mut self,
        policy: &Policy,
        temperature: Option<u8>,
        now: u64,
    ) -> Option<[Mode; 2]> {
        let modes = if !policy.curve_enabled {
            [
                manual(&policy.cpu_fan_mode, policy.cpu_fan_pwm),
                manual(&policy.gpu_fan_mode, policy.gpu_fan_pwm),
            ]
        } else if let Some(temp @ 1..=110) = temperature {
            let percent = if temp >= 95 {
                100
            } else {
                policy.percent(temp)
            };
            let pwm = (percent as u16 * 3 / 2) as u8;
            let target = [Mode::Custom(pwm); 2];
            if let Some([Mode::Custom(previous), _]) = self.applied {
                if pwm != previous && temp < 95 {
                    if temp.abs_diff(self.applied_temp) < 3 && pwm.abs_diff(previous) < 5 {
                        self.pending = None;
                        return self.refresh(now);
                    }
                    let rise = pwm > previous;
                    if self.pending.map(|p| p.0) != Some(rise) {
                        self.pending = Some((rise, now));
                    }
                    let delay = if rise {
                        policy.curve_rise_delay
                    } else {
                        policy.curve_fall_delay
                    } as u64;
                    if now.saturating_sub(self.pending.unwrap().1) < delay {
                        return self.refresh(now);
                    }
                }
            }
            target
        } else {
            // An invalid or unavailable sensor must not leave a fixed low duty active.
            [Mode::Auto; 2]
        };
        self.pending = None;
        if self.applied != Some(modes) || now.saturating_sub(self.applied_at) >= 5 {
            Some(modes)
        } else {
            None
        }
    }

    fn refresh(&self, now: u64) -> Option<[Mode; 2]> {
        if now.saturating_sub(self.applied_at) >= 5 {
            self.applied
        } else {
            None
        }
    }

    pub fn committed(&mut self, modes: [Mode; 2], temperature: u8, now: u64) {
        if self.applied != Some(modes) {
            self.applied_temp = temperature;
        }
        self.applied = Some(modes);
        self.applied_at = now;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn policy() -> Policy {
        Policy {
            curve_enabled: true,
            curve_points: vec![[64, 50], [75, 67], [90, 77], [95, 100]],
            curve_rise_delay: 8,
            curve_fall_delay: 5,
            cpu_fan_mode: "自动".into(),
            cpu_fan_pwm: 60,
            gpu_fan_mode: "自动".into(),
            gpu_fan_pwm: 60,
        }
    }

    #[test]
    fn interpolation_matches_existing_curve_and_ec_scaling() {
        let p = policy();
        p.validate().unwrap();
        assert_eq!(
            [
                p.percent(40),
                p.percent(64),
                p.percent(70),
                p.percent(71),
                p.percent(75),
                p.percent(90),
                p.percent(100)
            ],
            [50, 50, 59, 60, 67, 77, 100]
        );
        assert_eq!(manual("自定义", 67), Mode::Custom(100));
        assert_eq!(manual("满速", 0), Mode::Full);
    }

    #[test]
    fn delays_are_sustained_and_reversal_resets_the_pending_change() {
        let p = policy();
        let mut c = Controller::default();
        let initial = c.plan(&p, Some(60), 0).unwrap();
        c.committed(initial, 60, 0);
        assert_eq!(c.plan(&p, Some(75), 1), None);
        assert_eq!(c.plan(&p, Some(75), 4), None);
        assert_eq!(c.plan(&p, Some(60), 5), Some(initial));
        c.committed(initial, 60, 5);
        assert_eq!(c.plan(&p, Some(75), 6), None);
        assert_eq!(c.plan(&p, Some(75), 13), Some(initial));
        c.committed(initial, 60, 13);
        let high = c.plan(&p, Some(75), 14).unwrap();
        assert_eq!(high, [Mode::Custom(100); 2]);
        c.committed(high, 75, 14);
        assert_eq!(c.plan(&p, Some(60), 15), None);
        let low = c.plan(&p, Some(60), 20).unwrap();
        assert_eq!(low, initial);
    }

    #[test]
    fn critical_temperature_and_sensor_failure_bypass_delay() {
        let p = policy();
        let mut c = Controller::default();
        let low = c.plan(&p, Some(50), 0).unwrap();
        c.committed(low, 50, 0);
        assert_eq!(c.plan(&p, Some(95), 1), Some([Mode::Custom(150); 2]));
        assert_eq!(c.plan(&p, None, 1), Some([Mode::Auto; 2]));
        assert_eq!(c.plan(&p, Some(255), 1), Some([Mode::Auto; 2]));
    }

    #[test]
    fn malformed_policy_is_rejected_and_manual_mode_is_preserved() {
        let mut p = policy();
        p.curve_points[1][0] = 64;
        assert!(p.validate().is_err());
        p = policy();
        p.curve_fall_delay = 301;
        assert!(p.validate().is_err());
        p = policy();
        p.curve_enabled = false;
        p.cpu_fan_mode = "自定义".into();
        p.cpu_fan_pwm = 60;
        p.gpu_fan_mode = "满速".into();
        assert_eq!(
            Controller::default().plan(&p, None, 0),
            Some([Mode::Custom(90), Mode::Full])
        );
    }
}
