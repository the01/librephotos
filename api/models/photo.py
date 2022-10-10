import hashlib
import json
import numbers
import os
from fractions import Fraction
from io import BytesIO

import numpy as np
import PIL
from django.contrib.postgres.fields import ArrayField
from django.core.files.base import ContentFile
from django.db import models
from django.db.models import Q
from django.db.utils import IntegrityError

import api.date_time_extractor as date_time_extractor
import api.models
import api.util as util
from api.exif_tags import Tags
from api.im2txt.sample import im2txt
from api.models.user import User, get_deleted_user
from api.places365.places365 import place365_instance
from api.semantic_search.semantic_search import semantic_search_instance
from api.thumbnails import (
    createAnimatedThumbnail,
    createThumbnail,
    createThumbnailForVideo,
    doesStaticThumbnailExists,
    doesVideoThumbnailExists,
)
from api.util import get_metadata, logger


class VisiblePhotoManager(models.Manager):
    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(Q(hidden=False) & Q(aspect_ratio__isnull=False) & Q(deleted=False))
        )


class Photo(models.Model):
    image_paths = models.JSONField(default=list)
    image_hash = models.CharField(primary_key=True, max_length=64, null=False)

    thumbnail_big = models.ImageField(upload_to="thumbnails_big")

    square_thumbnail = models.ImageField(upload_to="square_thumbnails")
    square_thumbnail_small = models.ImageField(upload_to="square_thumbnails_small")

    aspect_ratio = models.FloatField(blank=True, null=True)

    added_on = models.DateTimeField(null=False, blank=False, db_index=True)

    exif_gps_lat = models.FloatField(blank=True, null=True)
    exif_gps_lon = models.FloatField(blank=True, null=True)
    exif_timestamp = models.DateTimeField(blank=True, null=True, db_index=True)

    exif_json = models.JSONField(blank=True, null=True)

    geolocation_json = models.JSONField(blank=True, null=True, db_index=True)
    captions_json = models.JSONField(blank=True, null=True, db_index=True)

    dominant_color = models.TextField(blank=True, null=True)

    search_captions = models.TextField(blank=True, null=True, db_index=True)
    search_location = models.TextField(blank=True, null=True, db_index=True)

    timestamp = models.DateTimeField(blank=True, null=True, db_index=True)
    rating = models.IntegerField(default=0, db_index=True)
    deleted = models.BooleanField(default=False, db_index=True)
    hidden = models.BooleanField(default=False, db_index=True)
    video = models.BooleanField(default=False)
    video_length = models.TextField(blank=True, null=True)
    size = models.IntegerField(default=0)
    fstop = models.FloatField(blank=True, null=True)
    focal_length = models.FloatField(blank=True, null=True)
    iso = models.IntegerField(blank=True, null=True)
    shutter_speed = models.TextField(blank=True, null=True)
    camera = models.TextField(blank=True, null=True)
    lens = models.TextField(blank=True, null=True)
    width = models.IntegerField(default=0)
    height = models.IntegerField(default=0)
    focalLength35Equivalent = models.IntegerField(blank=True, null=True)
    subjectDistance = models.FloatField(blank=True, null=True)
    digitalZoomRatio = models.FloatField(blank=True, null=True)

    owner = models.ForeignKey(
        User, on_delete=models.SET(get_deleted_user), default=None
    )

    shared_to = models.ManyToManyField(User, related_name="photo_shared_to")

    public = models.BooleanField(default=False, db_index=True)
    clip_embeddings = ArrayField(
        models.FloatField(blank=True, null=True), size=512, null=True
    )
    clip_embeddings_magnitude = models.FloatField(blank=True, null=True)

    objects = models.Manager()
    visible = VisiblePhotoManager()

    _loaded_values = {}

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)

        # save original values, when model is loaded from database,
        # in a separate attribute on the model
        instance._loaded_values = dict(zip(field_names, values))

        return instance

    def save(
        self,
        force_insert=False,
        force_update=False,
        using=None,
        update_fields=None,
        save_metadata=True,
    ):
        modified_fields = [
            field_name
            for field_name, value in self._loaded_values.items()
            if value != getattr(self, field_name)
        ]
        user = User.objects.get(username=self.owner)
        if save_metadata and user.save_metadata_to_disk != User.SaveMetadataToDisk.OFF:
            self._save_metadata(
                modified_fields,
                user.save_metadata_to_disk == User.SaveMetadataToDisk.SIDECAR_FILE,
            )
        return super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def _save_metadata(self, modified_fields=None, use_sidecar=True):
        tags_to_write = {}
        if modified_fields is None or "rating" in modified_fields:
            tags_to_write[Tags.RATING] = self.rating
        if "timestamp" in modified_fields:
            # To-Do: Only works for files and not for the sidecar file
            tags_to_write[Tags.DATE_TIME] = self.timestamp
        if tags_to_write:
            util.write_metadata(
                self.image_paths[0], tags_to_write, use_sidecar=use_sidecar
            )

    def _generate_md5(self):
        hash_md5 = hashlib.md5()
        with open(self.image_paths[0], "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        self.image_hash = hash_md5.hexdigest() + str(self.owner.id)
        self.save()

    def _generate_captions_im2txt(self, commit=True):
        image_path = self.thumbnail_big.path
        captions = self.captions_json
        search_captions = self.search_captions
        try:
            caption = im2txt(image_path)
            caption = (
                caption.replace("<start>", "").replace("<end>", "").strip().lower()
            )
            captions["im2txt"] = caption
            self.captions_json = captions
            # todo: handle duplicate captions
            self.search_captions = search_captions + caption
            if commit:
                self.save()
            util.logger.info(
                "generated im2txt captions for image %s. caption: %s"
                % (image_path, caption)
            )
            return True
        except Exception:
            util.logger.warning(
                "could not generate im2txt captions for image %s" % image_path
            )
            return False

    def _generate_clip_embeddings(self, commit=True):
        image_path = self.thumbnail_big.path
        if not self.clip_embeddings and image_path:
            try:
                img_emb, magnitude = semantic_search_instance.calculate_clip_embeddings(
                    image_path
                )
                self.clip_embeddings = img_emb
                self.clip_embeddings_magnitude = magnitude
                if commit:
                    self.save()
                util.logger.info(
                    "generated clip embeddings for image %s." % (image_path)
                )
            except Exception:
                util.logger.exception(
                    "could not generate clip embeddings for image %s" % image_path
                )

    def _generate_captions(self, commit):
        image_path = self.thumbnail_big.path
        captions = {}

        # places365
        try:
            confidence = self.owner.confidence
            res_places365 = place365_instance.inference_places365(
                image_path, confidence
            )
            captions["places365"] = res_places365
            self.captions_json = captions
            if self.search_captions:
                self.search_captions = (
                    self.search_captions
                    + " , "
                    + " , ".join(
                        res_places365["categories"] + [res_places365["environment"]]
                    )
                )
            else:
                self.search_captions = " , ".join(
                    res_places365["categories"] + [res_places365["environment"]]
                )
            if commit:
                self.save()
            util.logger.info(
                "generated places365 captions for image %s." % (image_path)
            )
        except Exception:
            util.logger.exception(
                "could not generate places365 captions for image %s" % image_path
            )

    def _generate_thumbnail(self, commit=True):
        if not doesStaticThumbnailExists("thumbnails_big", self.image_hash):
            if not self.video:
                createThumbnail(
                    inputPath=self.image_paths[0],
                    outputHeight=1080,
                    outputPath="thumbnails_big",
                    hash=self.image_hash,
                    fileType=".webp",
                )
            else:
                createThumbnailForVideo(
                    inputPath=self.image_paths[0],
                    outputPath="thumbnails_big",
                    hash=self.image_hash,
                    fileType=".webp",
                )

        if not self.video and not doesStaticThumbnailExists(
            "square_thumbnails", self.image_hash
        ):
            createThumbnail(
                inputPath=self.image_paths[0],
                outputHeight=500,
                outputPath="square_thumbnails",
                hash=self.image_hash,
                fileType=".webp",
            )
        if self.video and not doesVideoThumbnailExists(
            "square_thumbnails", self.image_hash
        ):
            createAnimatedThumbnail(
                inputPath=self.image_paths[0],
                outputHeight=500,
                outputPath="square_thumbnails",
                hash=self.image_hash,
                fileType=".mp4",
            )

        if not self.video and not doesStaticThumbnailExists(
            "square_thumbnails_small", self.image_hash
        ):
            createThumbnail(
                inputPath=self.image_paths[0],
                outputHeight=250,
                outputPath="square_thumbnails_small",
                hash=self.image_hash,
                fileType=".webp",
            )
        if self.video and not doesVideoThumbnailExists(
            "square_thumbnails_small", self.image_hash
        ):
            createAnimatedThumbnail(
                inputPath=self.image_paths[0],
                outputHeight=250,
                outputPath="square_thumbnails_small",
                hash=self.image_hash,
                fileType=".mp4",
            )
        filetype = ".webp"
        if self.video:
            filetype = ".mp4"
        self.thumbnail_big.name = os.path.join(
            "thumbnails_big", self.image_hash + ".webp"
        ).strip()
        self.square_thumbnail.name = os.path.join(
            "square_thumbnails", self.image_hash + filetype
        ).strip()
        self.square_thumbnail_small.name = os.path.join(
            "square_thumbnails_small", self.image_hash + filetype
        ).strip()
        if commit:
            self.save()

    def _find_album_place(self):
        return api.models.album_place.AlbumPlace.objects.filter(
            Q(photos__in=[self])
        ).all()

    def _find_album_date(self):
        old_album_date = None
        if self.exif_timestamp:
            possible_old_album_date = api.models.album_date.get_album_date(
                date=self.exif_timestamp.date(), owner=self.owner
            )
            if (
                possible_old_album_date is not None
                and possible_old_album_date.photos.filter(
                    image_hash=self.image_hash
                ).exists
            ):
                old_album_date = possible_old_album_date
        else:
            possible_old_album_date = api.models.album_date.get_album_date(
                date=None, owner=self.owner
            )
            if (
                possible_old_album_date is not None
                and possible_old_album_date.photos.filter(
                    image_hash=self.image_hash
                ).exists
            ):
                old_album_date = possible_old_album_date
        return old_album_date

    def _calculate_aspect_ratio(self, commit=True):
        height, width = get_metadata(
            self.thumbnail_big.path,
            tags=[Tags.IMAGE_HEIGHT, Tags.IMAGE_WIDTH],
            try_sidecar=False,
        )
        self.aspect_ratio = round(width / height, 2)

        if commit:
            self.save()

    def _extract_date_time_from_exif(self, commit=True):
        def exif_getter(tags):
            return get_metadata(self.image_paths[0], tags=tags, try_sidecar=True)

        datetime_config = json.loads(self.owner.datetime_rules)
        extracted_local_time = date_time_extractor.extract_local_date_time(
            self.image_paths[0],
            date_time_extractor.as_rules(datetime_config),
            exif_getter,
            self.exif_gps_lat,
            self.exif_gps_lon,
            self.owner.default_timezone,
            self.timestamp,
        )

        old_album_date = self._find_album_date()
        if self.exif_timestamp != extracted_local_time:
            self.exif_timestamp = extracted_local_time

        if old_album_date is not None:
            old_album_date.photos.remove(self)
            old_album_date.save()

        album_date = None

        if self.exif_timestamp:
            album_date = api.models.album_date.get_or_create_album_date(
                date=self.exif_timestamp.date(), owner=self.owner
            )
            album_date.photos.add(self)
        else:
            album_date = api.models.album_date.get_or_create_album_date(
                date=None, owner=self.owner
            )
            album_date.photos.add(self)

        if commit:
            self.save()
        album_date.save()

    def _geolocate_mapbox(self, commit=True):
        old_gps_lat = self.exif_gps_lat
        old_gps_lon = self.exif_gps_lon
        new_gps_lat, new_gps_lon = get_metadata(
            self.image_paths[0],
            tags=[Tags.LATITUDE, Tags.LONGITUDE],
            try_sidecar=True,
        )
        old_album_places = self._find_album_place()
        # Skip if it hasn't changed or is null
        if not new_gps_lat or not new_gps_lon:
            return
        if (
            old_gps_lat == float(new_gps_lat)
            and old_gps_lon == float(new_gps_lon)
            and old_album_places.count() != 0
            and self.geolocation_json
        ):
            return
        self.exif_gps_lon = float(new_gps_lon)
        self.exif_gps_lat = float(new_gps_lat)
        if commit:
            self.save()
        # Skip if the request fails or is empty
        res = None
        try:
            res = util.mapbox_reverse_geocode(new_gps_lat, new_gps_lon)
            if not res or len(res) == 0:
                return
            if "features" not in res.keys():
                return
        except Exception:
            util.logger.exception("something went wrong with geolocating")
            return

        self.geolocation_json = res

        if "search_text" in res.keys():
            if self.search_location:
                self.search_location = self.search_location + " " + res["search_text"]
            else:
                self.search_location = res["search_text"]
        # Delete photo from album places if location has changed

        if old_album_places is not None:
            for old_album_place in old_album_places:
                old_album_place.photos.remove(self)
                old_album_place.save()
        # Add photo to new album places
        for geolocation_level, feature in enumerate(self.geolocation_json["features"]):
            if "text" not in feature.keys() or feature["text"].isnumeric():
                continue
            album_place = api.models.album_place.get_album_place(
                feature["text"], owner=self.owner
            )
            if album_place.photos.filter(image_hash=self.image_hash).count() == 0:
                album_place.geolocation_level = (
                    len(self.geolocation_json["features"]) - geolocation_level
                )
            album_place.photos.add(self)
            album_place.save()

        if commit:
            self.save()

    def _add_location_to_album_dates(self):
        if not self.geolocation_json:
            return
        album_date = self._find_album_date()
        city_name = [
            f["text"]
            for f in self.geolocation_json["features"][1:-1]
            if not f["text"].isdigit()
        ][0]
        if album_date.location and len(album_date.location) > 0:
            prev_value = album_date.location
            new_value = prev_value
            if city_name not in prev_value["places"]:
                new_value["places"].append(city_name)
                new_value["places"] = list(set(new_value["places"]))
                album_date.location = new_value
        else:
            album_date.location = {"places": [city_name]}
        # Safe geolocation_json
        album_date.save()

    def _extract_video_length(self, commit=True):
        if not self.video:
            return
        (video_length,) = get_metadata(
            self.image_paths[0], tags=[Tags.QUICKTIME_DURATION], try_sidecar=True
        )
        logger.debug(f"Extracted rating for {self.image_paths[0]}: {video_length}")
        self.video_length = video_length
        if commit:
            self.save()

    def _extract_exif_data(self, commit=True):
        (
            size,
            fstop,
            focal_length,
            iso,
            shutter_speed,
            camera,
            lens,
            width,
            height,
            focalLength35Equivalent,
            subjectDistance,
            digitalZoomRatio,
        ) = get_metadata(  # noqa: E501
            self.image_paths[0],
            tags=[
                Tags.FILE_SIZE,
                Tags.FSTOP,
                Tags.FOCAL_LENGTH,
                Tags.ISO,
                Tags.EXPOSURE_TIME,
                Tags.CAMERA,
                Tags.LENS,
                Tags.IMAGE_WIDTH,
                Tags.IMAGE_HEIGHT,
                Tags.FOCAL_LENGTH_35MM,
                Tags.SUBJECT_DISTANCE,
                Tags.DIGITAL_ZOOM_RATIO,
            ],
            try_sidecar=True,
        )
        self.size = size
        self.fstop = fstop
        self.focal_length = focal_length
        self.iso = iso
        if shutter_speed and isinstance(shutter_speed, numbers.Number):
            self.shutter_speed = str(Fraction(shutter_speed).limit_denominator(1000))
        self.camera = camera
        self.lens = lens
        self.width = width
        self.height = height
        self.focalLength35Equivalent = focalLength35Equivalent
        if subjectDistance and isinstance(digitalZoomRatio, numbers.Number):
            self.subjectDistance = subjectDistance
        if digitalZoomRatio and isinstance(digitalZoomRatio, numbers.Number):
            self.digitalZoomRatio = digitalZoomRatio
        if commit:
            self.save()

    def _extract_rating(self, commit=True):
        (rating,) = get_metadata(
            self.image_paths[0], tags=[Tags.RATING], try_sidecar=True
        )
        if rating is not None:
            # Only change rating if the tag was found
            logger.debug(f"Extracted rating for {self.image_paths[0]}: {rating}")
            self.rating = rating
            if commit:
                self.save(save_metadata=False)

    def _extract_faces(self, second_try=False):
        import face_recognition

        try:
            qs_unknown_person = api.models.person.Person.objects.filter(
                Q(name="unknown") | Q(name=api.models.person.Person.UNKNOWN_PERSON_NAME)
            )
            if qs_unknown_person.count() == 0:
                unknown_person = api.models.person.get_unknown_person(owner=self.owner)
            else:
                unknown_person = qs_unknown_person[0]

            unknown_cluster: api.models.cluster.Cluster = (
                api.models.cluster.get_unknown_cluster()
            )
            image = np.array(PIL.Image.open(self.thumbnail_big.path))

            face_locations = []
            try:
                face_locations = face_recognition.face_locations(image)
            except Exception:
                logger.debug(
                    f"Can't extract face information on photo: {self.image_paths[0]}"
                )

            if len(face_locations) > 0:
                face_encodings = face_recognition.face_encodings(
                    image, known_face_locations=face_locations
                )
                for idx_face, face in enumerate(zip(face_encodings, face_locations)):
                    face_encoding = face[0]
                    face_location = face[1]
                    top, right, bottom, left = face_location
                    face_image = image[top:bottom, left:right]
                    face_image = PIL.Image.fromarray(face_image)

                    image_path = self.image_hash + "_" + str(idx_face) + ".jpg"

                    margin = 2
                    existing_faces = api.models.face.Face.objects.filter(
                        photo=self,
                        location_top__lte=face_location[0] + margin,
                        location_top__gte=face_location[0] - margin,
                        location_right__lte=face_location[1] + margin,
                        location_right__gte=face_location[1] - margin,
                        location_bottom__lte=face_location[2] + margin,
                        location_bottom__gte=face_location[2] - margin,
                        location_left__lte=face_location[3] + margin,
                        location_left__gte=face_location[3] - margin,
                    )

                    if existing_faces.count() != 0:
                        continue

                    face = api.models.face.Face(
                        image_path=image_path,
                        photo=self,
                        timestamp=self.exif_timestamp,
                        location_top=face_location[0],
                        location_right=face_location[1],
                        location_bottom=face_location[2],
                        location_left=face_location[3],
                        encoding=face_encoding.tobytes().hex(),
                        person=unknown_person,
                        cluster=unknown_cluster,
                    )

                    face_io = BytesIO()
                    face_image.save(face_io, format="JPEG")
                    face.image.save(face.image_path, ContentFile(face_io.getvalue()))
                    face_io.close()
                    face.save()
                logger.info(
                    "image {}: {} face(s) saved".format(
                        self.image_hash, len(face_locations)
                    )
                )

        except IntegrityError:
            # When using multiple processes, then we can save at the same time, which leads to this error
            if self.image_paths != []:
                # print out the location of the image only if we have a path
                logger.info("image {}: rescan face failed".format(self.image_paths[0]))
            if not second_try:
                self._extract_faces(True)
            else:
                if self.image_paths != []:
                    logger.error(
                        "image {}: rescan face failed".format(self.image_paths[0])
                    )
                else:
                    logger.error("image {}: rescan face failed".format(self))

    def _add_to_album_thing(self):
        if (
            type(self.captions_json) is dict
            and "places365" in self.captions_json.keys()
        ):
            for attribute in self.captions_json["places365"]["attributes"]:
                album_thing = api.models.album_thing.get_album_thing(
                    title=attribute, owner=self.owner
                )
                if album_thing.photos.filter(image_hash=self.image_hash).count() == 0:
                    album_thing.photos.add(self)
                    album_thing.thing_type = "places365_attribute"
                    album_thing.save()
            for category in self.captions_json["places365"]["categories"]:
                album_thing = api.models.album_thing.get_album_thing(
                    title=category, owner=self.owner
                )
                if album_thing.photos.filter(image_hash=self.image_hash).count() == 0:
                    album_thing = api.models.album_thing.get_album_thing(
                        title=category, owner=self.owner
                    )
                    album_thing.photos.add(self)
                    album_thing.thing_type = "places365_category"
                    album_thing.save()

    def _check_image_paths(self):
        exisiting_image_paths = []
        for image_path in self.image_paths:
            if os.path.exists(image_path):
                exisiting_image_paths.append(image_path)
        self.image_paths = exisiting_image_paths
        self.save()

    def _get_dominant_color(self, palette_size=16):
        # Skip if it's already calculated
        if self.dominant_color:
            return
        try:
            # Resize image to speed up processing
            img = PIL.Image.open(self.thumbnail_big.path)
            img.thumbnail((100, 100))

            # Reduce colors (uses k-means internally)
            paletted = img.convert("P", palette=PIL.Image.ADAPTIVE, colors=palette_size)

            # Find the color that occurs most often
            palette = paletted.getpalette()
            color_counts = sorted(paletted.getcolors(), reverse=True)
            palette_index = color_counts[0][1]
            dominant_color = palette[palette_index * 3 : palette_index * 3 + 3]
            self.dominant_color = dominant_color
            self.save()
        except Exception:
            print("Cannot calculate dominant color {} object".format(self))

    def __str__(self):
        return "%s" % self.image_hash

    def manual_delete(self):
        for path in self.image_paths:
            if os.path.isfile(path):
                logger.info("Removing photo {}".format(path))
                os.remove(path)
        # To-Do: Handle wrong file permissions
        return self.delete()

    def delete_duplicate(self, duplicate_path):
        if duplicate_path not in self.image_paths:
            logger.info("Path is not valid: {}".format(duplicate_path))
            return False
        if not os.path.isfile(duplicate_path):
            logger.info("Path does not lead to a valid file: {}".format(duplicate_path))
            self.image_paths.remove(duplicate_path)
            self.save()
            return False
        # To-Do: Handle wrong file permissions
        logger.info("Removing photo {}".format(duplicate_path))
        os.remove(duplicate_path)
        self.image_paths.remove(duplicate_path)
        self.save()
        return True
