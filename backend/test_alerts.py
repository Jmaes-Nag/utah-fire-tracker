import unittest
import math
from shapely.geometry import Point, Polygon
from shapely.affinity import translate

class TestAlertsEngine(unittest.TestCase):
    def test_directional_math(self):
        # Fire coordinates
        fire_lat = 40.7608
        fire_lon = -111.8910
        
        # Wind: South wind (180 deg) at 20 mph
        # Downwind heading: (180 + 180) % 360 = 0 deg (North)
        # Trig angle: 90 - 0 = 90 deg
        # dx should be 0, dy should be positive (North shift)
        wind_dir_deg = 180
        wind_speed_mph = 20.0
        
        DEG_PER_MILE = 0.0145
        
        downwind_deg = (wind_dir_deg + 180) % 360
        trig_deg = 90 - downwind_deg
        trig_rad = math.radians(trig_deg)
        
        shift_miles = wind_speed_mph * 0.2
        shift_deg = shift_miles * DEG_PER_MILE
        dx = math.cos(trig_rad) * shift_deg
        dy = math.sin(trig_rad) * shift_deg
        
        # Assert dx is 0 and dy is positive (shifting North)
        self.assertAlmostEqual(dx, 0.0, places=5)
        self.assertGreater(dy, 0.0)
        
        # Verify translating shapely point works as expected
        fire_point = Point(fire_lon, fire_lat)
        shifted_point = translate(fire_point, xoff=dx, yoff=dy)
        self.assertAlmostEqual(shifted_point.x, fire_lon, places=5)
        self.assertGreater(shifted_point.y, fire_lat)
        
    def test_intersection_logic(self):
        # Setup: Fire is at 40.66, -111.89
        # City polygon is around 40.75, -111.89 (about 6.2 miles North)
        # 5-mile base buffer: no intersection (5 * 0.0145 = 0.0725 deg ~ 5 miles)
        # With 20mph South wind blowing North, the shift is 4 miles (4 * 0.0145 = 0.058 deg).
        # Shifted point is at 40.718. The buffer extends to 40.79. It should intersect!
        
        fire_point = Point(-111.8910, 40.6608)
        city_polygon = Polygon([
            (-111.90, 40.75),
            (-111.88, 40.75),
            (-111.88, 40.77),
            (-111.90, 40.77),
            (-111.90, 40.75)
        ])
        
        # Scenario 1: No wind
        threat_zone_no_wind = fire_point.buffer(5.0 * 0.0145)
        self.assertFalse(threat_zone_no_wind.intersects(city_polygon))
        
        # Scenario 2: South wind (180 deg) at 20 mph (blows North)
        dx = 0.0
        dy = 20.0 * 0.2 * 0.0145 # 0.058 deg North shift
        shifted_fire_point = translate(fire_point, xoff=dx, yoff=dy)
        threat_zone_wind = fire_point.union(shifted_fire_point).buffer(5.0 * 0.0145)
        
        self.assertTrue(threat_zone_wind.intersects(city_polygon))

    def test_aqi_breakpoints(self):
        from app.main import calculate_pm25_aqi
        # Good range: 0.0 to 9.0 PM2.5 -> 0 to 50 AQI
        self.assertEqual(calculate_pm25_aqi(0.0), 0)
        self.assertEqual(calculate_pm25_aqi(4.5), 25)
        self.assertEqual(calculate_pm25_aqi(9.0), 50)
        
        # Moderate range: 9.1 to 35.4 -> 51 to 100
        self.assertEqual(calculate_pm25_aqi(9.1), 51)
        self.assertEqual(calculate_pm25_aqi(22.25), 75)
        self.assertEqual(calculate_pm25_aqi(35.4), 100)
        
        # Unhealthy for Sensitive Groups: 35.5 to 55.4 -> 101 to 150
        self.assertEqual(calculate_pm25_aqi(35.5), 101)
        self.assertEqual(calculate_pm25_aqi(45.45), 126)
        self.assertEqual(calculate_pm25_aqi(55.4), 150)
        
        # Hazardous upper boundary cap
        self.assertEqual(calculate_pm25_aqi(500.0), 500)
        self.assertEqual(calculate_pm25_aqi(600.0), 500)

if __name__ == '__main__':
    unittest.main()
