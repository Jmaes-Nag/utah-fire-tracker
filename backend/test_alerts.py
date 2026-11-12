import unittest
from shapely.geometry import box
from app.main import calculate_pm25_aqi, threat_geometry


class TestAlertsEngine(unittest.TestCase):
    def test_south_wind_projects_north_and_retains_fire_buffer(self):
        base, plume, _, _ = threat_geometry(40.6608, -111.8910, 20.0, 180.0)
        city = box(-111.90, 40.75, -111.88, 40.77)
        self.assertFalse(base.intersects(city))
        self.assertTrue(plume.intersects(city))
        self.assertTrue(plume.covers(base))

    def test_calm_weather_uses_base_buffer(self):
        base, plume, _, _ = threat_geometry(40.6608, -111.8910, 0.0, None)
        self.assertTrue(base.equals(plume))

    def test_aqi_breakpoints(self):
        # EPA concentration truncation precedes interpolation and AQI rounding.
        expected = {0.0: 0, 4.5: 25, 9.0: 50, 9.1: 51, 22.25: 75,
                    35.4: 100, 35.5: 101, 45.45: 125, 55.4: 150,
                    225.5: 301, 325.4: 500, 500.0: 500, 600.0: 500}
        for concentration, aqi in expected.items():
            with self.subTest(concentration=concentration):
                self.assertEqual(calculate_pm25_aqi(concentration), aqi)


if __name__ == '__main__':
    unittest.main()
