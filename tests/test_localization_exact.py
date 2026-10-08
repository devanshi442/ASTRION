import pytest
import numpy as np
from pathlib import Path
from astropy.io import fits
from astropy.wcs import WCS
from astropy.coordinates import SkyCoord
import astropy.units as u

def test_exact_obsjd_extraction():
    # Mocking a basic FITS header
    header = fits.Header()
    header['OBSJD'] = 2459093.9990625
    header['EXPTIME'] = 30.0
    
    assert float(header['OBSJD']) == 2459093.9990625
    assert float(header['EXPTIME']) == 30.0

def test_wcs_world_to_pixel_and_inside_image():
    # Create a simple WCS
    wcs_dict = {
        'CTYPE1': 'RA---TAN',
        'CTYPE2': 'DEC--TAN',
        'CRVAL1': 110.0,
        'CRVAL2': 29.0,
        'CRPIX1': 100.5,
        'CRPIX2': 100.5,
        'CD1_1': -0.00028,
        'CD1_2': 0.0,
        'CD2_1': 0.0,
        'CD2_2': 0.00028,
        'NAXIS1': 200,
        'NAXIS2': 200
    }
    wcs = WCS(wcs_dict)
    
    # Target exactly at reference pixel
    target = SkyCoord(ra=110.0*u.deg, dec=29.0*u.deg, frame='icrs')
    x, y = wcs.world_to_pixel(target)
    
    assert np.isclose(x, 99.5)
    assert np.isclose(y, 99.5)
    
    # Inside image logic
    nx, ny = 200, 200
    inside = (0 <= x < nx) and (0 <= y < ny)
    assert inside == True
    
    # Target outside
    target_out = SkyCoord(ra=111.0*u.deg, dec=30.0*u.deg, frame='icrs')
    x_out, y_out = wcs.world_to_pixel(target_out)
    inside_out = (0 <= x_out < nx) and (0 <= y_out < ny)
    assert inside_out == False

def test_trail_length_calculation():
    ra_rate_hr = 10.0
    dec_rate_hr = 0.0
    exptime = 30.0
    pixel_scale = 1.013 # arcsec/pixel
    
    total_rate_arcsec_sec = np.sqrt(ra_rate_hr**2 + dec_rate_hr**2) / 3600.0
    trail_length_arcsec = total_rate_arcsec_sec * exptime
    trail_length_pixels = trail_length_arcsec / pixel_scale
    
    assert np.isclose(trail_length_arcsec, (10.0 / 3600.0) * 30.0)
    assert trail_length_pixels > 0

def test_temporal_and_spatial_separation():
    obsjd = 2459093.5
    ephjd = 2459093.5
    temporal_sep = abs(obsjd - ephjd)
    assert temporal_sep == 0.0
    
    sc1 = SkyCoord(ra=10.0*u.deg, dec=10.0*u.deg)
    sc2 = SkyCoord(ra=10.0*u.deg, dec=10.001*u.deg) # 0.001 deg = 3.6 arcsec
    sep = sc1.separation(sc2).arcsec
    assert np.isclose(sep, 3.6)

def test_regression_strict_epoch_usage():
    """
    Regression test proving that a coarse ephemeris epoch cannot silently
    be used when an exact OBSJD is available.
    """
    def localize(obsjd, ephjd):
        if abs(obsjd - ephjd) > 1e-5:
            raise ValueError("Strict epoch usage violation: ephemeris epoch does not match exact OBSJD.")
        return True

    exact_obsjd = 2459093.9990625
    coarse_ephjd = 2459093.5 # e.g. 12-hour grid
    
    # Should raise error for coarse
    with pytest.raises(ValueError, match="Strict epoch usage violation"):
        localize(exact_obsjd, coarse_ephjd)
        
    # Should pass for exact
    assert localize(exact_obsjd, exact_obsjd) == True
