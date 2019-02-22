#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Derive atmospheric correction coefficients for multiple images and optionally
# export corrected images.
# authors: Aman Verma, Preeti Rao
# -----------------------------------------------------------------------------

import collections
from pprint import pprint
import datetime
import math
import pickle

import ee

ee.Initialize()
from atmcorr.atmospheric import Atmospheric
from atmcorr.timeSeries import timeSeries


# AOI and type
TARGET = "forest"
GEOM = ee.Geometry.Rectangle(
    85.5268682942167402, 25.6240533612814261, 85.7263954375090407, 25.8241594034421382
)
# satellite missions,
MISSIONS = ["Sentinel2"]
# Change this to location of iLUTs
DIRPATH = "./files/iLUTs/S2A_MSI/Continental/view_zenith_0/"
# start and end of time series
START_DATE = "2016-11-19"  # YYYY-MM-DD
STOP_DATE = "2017-02-17"  # YYYY-MM-DD
NO_OF_BANDS = 13
# the following creates interpolated lookup tables.
_ = timeSeries(TARGET, GEOM, START_DATE, STOP_DATE, MISSIONS)

SRTM = ee.Image("CGIAR/SRTM90_V4")
# Shuttle Radar Topography mission covers *most* of the Earth
ALTITUDE = (
    SRTM.reduceRegion(reducer=ee.Reducer.mean(), geometry=GEOM.centroid())
    .get("elevation")
    .getInfo()
)
KM = ALTITUDE / 1000  # i.e. Py6S uses units of kilometers

# The Sentinel-2 image collection
S2 = (
    ee.ImageCollection("COPERNICUS/S2")
    .filterBounds(GEOM)
    .filterDate(START_DATE, STOP_DATE)
    .sort("system:time_start")
)
S2List = S2.toList(S2.size())  # must loop through lists

NO_OF_IMAGES = S2.size().getInfo()  # no. of images in the collection

AtmParams = collections.namedtuple("AtmParams", ("doy", "solar_z", "h2o", "o3", "aot"))


def atm_corr_image(imageInfo) -> AtmParams:
    """Retrieves atmospheric params from image.

    imageInfo is a dictionary created from an ee.Image object
    """
    # Python uses seconds, EE uses milliseconds:
    scene_date = datetime.datetime.utcfromtimestamp(
        imageInfo["system:time_start"] / 1000
    )
    dt1 = ee.Date(str(scene_date).rsplit(sep=" ")[0])

    atm_params = AtmParams(
        doy=scene_date.timetuple().tm_yday,
        solar_z=imageInfo["MEAN_SOLAR_ZENITH_ANGLE"],
        h2o=Atmospheric.water(GEOM, dt1).getInfo(),
        o3=Atmospheric.ozone(GEOM, dt1).getInfo(),
        aot=Atmospheric.aerosol(GEOM, dt1).getInfo(),
    )

    return atm_params


def get_corr_coef(atmParams):
    """Gets correction coefficients for each band in the image.

    Uses DIRPATH global variable
    Uses NO_OF_BANDS global variable
    Uses KM global variable
    Returns list of 2-length tuples
    """
    corr_coefs = []
    # string list with padding of 2
    band_nums = [str(img_idx).zfill(2) for img_idx in range(1, NO_OF_BANDS + 1)]
    for band in band_nums:
        filepath = DIRPATH + "S2A_MSI_" + band + ".ilut"
        with open(filepath, "rb") as ilut_file:
            iluTable = pickle.load(ilut_file)
        a, b = iluTable(
            atmParams.solar_z, atmParams.h2o, atmParams.o3, atmParams.aot, KM
        )
        elliptical_orbit_correction = (
            0.03275104 * math.cos(atmParams.doy / 59.66638337) + 0.96804905
        )
        a *= elliptical_orbit_correction
        b *= elliptical_orbit_correction
        corr_coefs.append((a, b))
    return corr_coefs


def toa_to_rad_multiplier(bandname, imageInfo, atmParams):
    """Returns a multiplier for converting TOA reflectance to radiance

    bandname is a string like 'B1'
    """
    ESUN = imageInfo["SOLAR_IRRADIANCE_" + bandname]
    # solar exoatmospheric spectral irradiance
    solar_angle_correction = math.cos(math.radians(atmParams["solar_z"]))
    # Earth-Sun distance (from day of year)
    d = 1 - 0.01672 * math.cos(0.9856 * (atmParams["doy"] - 4))
    # http://physics.stackexchange.com/questions/177949/earth-sun-distance-on-a-given-day-of-the-year
    # conversion factor
    multiplier = ESUN * solar_angle_correction / (math.pi * d ** 2)
    # at-sensor radiance

    return multiplier


def atm_corr_band(image, imageInfo, atmParams):
    """Atmospherically correct image

    Converts toa reflectance to radiance.
    Applies correction coefficients to get surface reflectance
    Returns ee.Image object
    """
    old_image = ee.Image(image).divide(10000)
    new_image = ee.Image()
    cor_coeff_list = get_corr_coef(atmParams)
    bandnames = old_image.bandNames().getInfo()
    for ii in range(NO_OF_BANDS):
        img_to_rad_multiplier = toa_to_rad_multiplier(
            bandnames[ii], imageInfo, atmParams
        )
        img_rad = old_image.select(bandnames[ii]).multiply(img_to_rad_multiplier)
        const_img_a = ee.Image.constant(cor_coeff_list[ii][0])
        const_img_b = ee.Image.constant(cor_coeff_list[ii][1])
        surface_refl = img_rad.subtract(const_img_a).divide(const_img_b)
        new_image = new_image.addBands(surface_refl)

    # unpack a list of the band indexes:
    return new_image.select(*list(range(NO_OF_BANDS)))


S2List_copy = S2List
corrected_images = ee.List([0])  # Can't init empty list so need a garbage element
export_list = []
coeff_list = []
for img_idx in range(NO_OF_IMAGES):
    img_info = S2List_copy.get(img_idx).getInfo()
    img_info_properties = img_info["properties"]
    atm_vars = atm_corr_image(img_info_properties)
    corr_coeffs = get_corr_coef(atm_vars)
    coeff_list.append(corr_coeffs)
    # Set to True to get an ee.List with the images and even export them to EE.
    EXPORT = False
    if EXPORT:
        img = atm_corr_band(
            ee.Image(S2List.get(img_idx)), img_info_properties, atm_vars
        )
        export = ee.batch.Export.image.toDrive(
            image=img,
            fileNamePrefix="sen2_" + str(img_idx),
            description="py",
            scale=10,
            folder="gee_img",
            maxPixels=1e13,
        )
        export_list.append(export)
        corrected_images = corrected_images.add(img)

# Need to remove the first element from the list (remeber?)
corrected_images = corrected_images.slice(1)

if EXPORT:
    for task in export_list:
        task.start()

with open("coeff_list.txt", "w") as f:
    pprint(coeff_list, stream=f)
