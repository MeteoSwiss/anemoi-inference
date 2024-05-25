# (C) Copyright 2024 European Centre for Medium-Range Weather Forecasts.
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.


import datetime
import logging
from functools import cached_property

import numpy as np
import torch

from .checkpoint import Checkpoint

LOG = logging.getLogger(__name__)


def forcing_and_constants(source, date, param):
    import climetlab as cml

    ds = cml.load_source(
        "constants",
        source,
        date=date,
        param=param,
    )

    assert len(ds) == len(param), (len(ds), len(param), date)

    return ds.to_numpy(dtype=np.float32)


class Runner:

    def __init__(self, checkpoint):

        if isinstance(checkpoint, str):
            checkpoint = Checkpoint(self, checkpoint)
        else:
            assert isinstance(checkpoint, Checkpoint)
            self.checkpoint = checkpoint

    def run(
        self,
        *,
        input_fields,
        lead_time,
        start_datetime,
        device,
        output_callback,
        autocast=torch.float16,
        stepper=None,
    ):

        input_fields = input_fields.sel(**self.checkpoint.select)
        input_fields = input_fields.order_by(**self.checkpoint.order_by)

        gridpoints = len(input_fields[0].grid_points()[0])

        LOG.info("Loading input: %d fields (lagged=%d)", len(input_fields), len(self.lagged))

        input_fields_numpy = input_fields.to_numpy(dtype=np.float32, reshape=False)
        input_fields_numpy = input_fields_numpy.reshape(
            len(self.lagged),
            len(input_fields) // len(self.lagged),
            gridpoints,
        )  # nlags, nparams, ngrid

        # Used to check if we cover the whole input, with no overlaps
        check = np.full(self.checkpoint.num_input_features, fill_value=False, dtype=np.bool_)

        kinds = np.full(self.checkpoint.num_input_features, fill_value="?", dtype=np.character)

        inputs = np.full(self.checkpoint.num_input_features, fill_value=False, dtype=np.bool_)

        # E.g cos_latitude
        computed_constant_mask = self.checkpoint.computed_constants_mask
        assert not np.any(check[computed_constant_mask]), check
        check[computed_constant_mask] = True
        kinds[computed_constant_mask] = "C"

        # E.g. lsm, orography
        constant_from_input_mask = self.checkpoint.constants_from_input_mask
        assert not np.any(check[constant_from_input_mask]), check
        check[constant_from_input_mask] = True
        kinds[constant_from_input_mask] = "K"
        inputs[constant_from_input_mask] = True

        # e.g. isolation
        computed_forcing_mask = self.checkpoint.computed_forcings_mask
        assert not np.any(check[computed_forcing_mask]), check
        check[computed_forcing_mask] = True
        kinds[computed_forcing_mask] = "F"

        # e.g 2t, 10u, 10v
        prognostic_input_mask = self.checkpoint.prognostic_input_mask
        assert not np.any(check[prognostic_input_mask]), check
        check[prognostic_input_mask] = True
        kinds[prognostic_input_mask] = "P"
        inputs[prognostic_input_mask] = True

        #
        if not np.all(check):
            for i, c in enumerate(check):
                if not c:
                    LOG.error(
                        "Missing %s %s %s",
                        i,
                        self.checkpoint.model_to_data[i],
                        self.checkpoint.index_to_variable[self.checkpoint.model_to_data[i]],
                    )
            raise RuntimeError("Missing fields")

        prognostic_data_from_retrieved_fields_mask = []
        constant_data_from_retrieved_fields_mask = []

        MARS = {False: " ", True: "X"}

        retrieved_fields_index = 0
        for i, c in enumerate(check):
            if inputs[i]:
                assert kinds[i].decode() in ("P", "K")
                if kinds[i].decode() == "P":
                    prognostic_data_from_retrieved_fields_mask.append(retrieved_fields_index)
                else:
                    constant_data_from_retrieved_fields_mask.append(retrieved_fields_index)
                retrieved_fields_index += 1

            if hasattr(self, "verbose") and self.verbose:
                print(
                    "{:4d} {:1s} {} {:4d} {:10s}".format(
                        i,
                        kinds[i].decode(),
                        MARS[inputs[i]],
                        self.checkpoint.model_to_data[i],
                        self.checkpoint.index_to_variable[self.checkpoint.model_to_data[i]],
                    )
                )

        prognostic_data_from_retrieved_fields_mask = np.array(prognostic_data_from_retrieved_fields_mask)
        constant_data_from_retrieved_fields_mask = np.array(constant_data_from_retrieved_fields_mask)

        # Build the input tensor

        input_tensor_numpy = np.full(
            shape=(
                len(self.lagged),
                self.checkpoint.num_input_features,
                gridpoints,
            ),
            fill_value=np.nan,
            dtype=np.float32,
        )  # nlags, nparams, ngrid

        # Check that the computed constant mask and the constant from input mask are disjoint
        # assert np.amax(prognostic_input_mask) < np.amin(constant_from_input_mask)

        input_tensor_numpy[:, prognostic_input_mask] = input_fields_numpy[:, prognostic_data_from_retrieved_fields_mask]

        input_tensor_numpy[:, constant_from_input_mask] = input_fields_numpy[
            :, constant_data_from_retrieved_fields_mask
        ]

        constants = forcing_and_constants(
            source=input_fields[:1],
            param=self.checkpoint.computed_constants,
            date=start_datetime,
        )

        for i in range(len(self.lagged)):
            input_tensor_numpy[i, computed_constant_mask] = constants

        for i in range(len(self.lagged)):
            forcings = forcing_and_constants(
                source=input_fields[:1],
                param=self.checkpoint.computed_forcings,
                date=start_datetime + datetime.timedelta(hours=self.lagged[i]),
            )
            input_tensor_numpy[i, computed_forcing_mask] = forcings

        LOG.info("Input tensor shape: %s", input_tensor_numpy.shape)

        imputable_variables = self.checkpoint.imputable_variables
        can_be_missing = set()
        # check for NaNs
        for i in range(input_tensor_numpy.shape[1]):
            name = self.checkpoint.index_to_variable[self.checkpoint.model_to_data[i]]
            has_missing = np.isnan(input_tensor_numpy[:, i, :]).any()
            is_imputable = name in imputable_variables
            if has_missing:
                can_be_missing.add(name)
                if not is_imputable:
                    model_index = self.checkpoint.model_to_data[i]
                    LOG.error(
                        "No imputation specified for NaNs in %s (%s %s)",
                        name,
                        i,
                        model_index,
                    )
                    raise ValueError(f"Field '{name}' has NaNs and is not marked as imputable")

        # with self.timer(f"Loading {self.checkpoint}"):
        if True:
            try:
                model = torch.load(
                    self.checkpoint.path,
                    map_location=device,
                ).to(device)
            except Exception:
                self.checkpoint.report_loading_error()
                raise

        model.eval()

        torch.set_grad_enabled(False)

        input_tensor_torch = torch.from_numpy(
            np.swapaxes(
                input_tensor_numpy,
                -2,
                -1,
            )[np.newaxis, ...]
        ).to(device)

        prognostic_output_mask = self.checkpoint.prognostic_output_mask
        diagnostic_output_mask = self.checkpoint.diagnostic_output_mask

        # autocast = {
        #     "16": torch.float16,
        #     "32": torch.float32,
        #     "b16": torch.bfloat16,
        #     "bfloat16": torch.bfloat16,
        #     "float16": torch.float16,
        #     "float32": torch.float32,
        # }[self.autocast]
        # device_type = "cuda"

        LOG.info("Using autocast %s", autocast)

        # Write dynamic fields
        def get_most_recent_datetime(input_fields):
            datetimes = [f.valid_datetime() for f in input_fields]
            latest = datetimes[-1]
            for d in datetimes:
                assert d <= latest, (datetimes, d, latest)
            return latest

        most_recent_datetime = get_most_recent_datetime(input_fields)
        reference_fields = [f for f in input_fields if f.valid_datetime() == most_recent_datetime]
        precip_template = reference_fields[self.checkpoint.variable_to_index["2t"]]

        accumulated_output = np.zeros(
            shape=(len(diagnostic_output_mask), gridpoints),
            dtype=np.float32,
        )

        if self.checkpoint.diagnostic_params:
            output_callback(
                input_fields,
                self.checkpoint.diagnostic_params,
                precip_template,
                accumulated_output[0].shape,
            )
        else:
            output_callback(input_fields)

        def add_ensemble_dim(func):
            def wrapper(x):
                x = x.unsqueeze(2)
                y = func(x)
                return y.squeeze_(2)

            return wrapper

        if True:  # self.add_ensemble_dimension:
            LOG.warning("🚨" * 80)
            LOG.warning("Adding ensemble dimension.")
            LOG.warning("If you are using that flags, your are using unsupported code")
            LOG.warning("that can be removed any time in the near future")
            LOG.warning("🚨" * 80)

            model.predict_step = add_ensemble_dim(model.predict_step)

        prognostic_params = self.checkpoint.prognostic_params

        # with self.stepper(self.hour_steps) as stepper:
        if True:
            for i in range(lead_time // self.hour_steps):
                step = (i + 1) * self.hour_steps

                # Predict next state of atmosphere
                with torch.autocast(device_type=device, dtype=autocast):
                    y_pred = model.predict_step(input_tensor_torch)

                # Detach tensor and squeeze
                output = np.squeeze(y_pred.cpu().numpy())

                prognostic_fields_numpy = output[:, prognostic_output_mask]
                if len(diagnostic_output_mask):
                    diagnostic_fields_numpy = output[:, diagnostic_output_mask]

                for n, (m, param) in enumerate(zip(prognostic_data_from_retrieved_fields_mask, prognostic_params)):
                    template = reference_fields[m]
                    assert template.valid_datetime() == most_recent_datetime, (
                        template.valid_datetime(),
                        most_recent_datetime,
                    )
                    output_callback(
                        prognostic_fields_numpy[:, n],
                        template=template,
                        step=step,
                        check_nans=True,  # param in can_be_missing,
                    )

                # Write diagnostic fields
                if len(diagnostic_output_mask):
                    for n, param in enumerate(self.checkpoint.diagnostic_params):
                        accumulated_output[n] += np.maximum(0, diagnostic_fields_numpy[:, n])
                        assert precip_template.valid_datetime() == most_recent_datetime, (
                            precip_template.valid_datetime(),
                            most_recent_datetime,
                        )
                        output_callback(
                            accumulated_output[n],
                            stepType="accum",
                            template=precip_template,
                            startStep=0,
                            endStep=step,
                            param=param,
                            check_nans=True,  # param in can_be_missing,
                        )

                # Next step

                prognostic_fields = y_pred[..., prognostic_output_mask]

                # Compute new forcing

                forcing = forcing_and_constants(
                    source=input_fields[:1],
                    param=self.checkpoint.computed_forcings,
                    date=start_datetime + datetime.timedelta(hours=step),
                )
                forcing = np.swapaxes(forcing[np.newaxis, np.newaxis, ...], -2, -1)
                forcing = torch.from_numpy(forcing).to(device)

                # Update dynamic tensor for next iteration
                input_tensor_torch = input_tensor_torch.roll(-1, dims=1)
                input_tensor_torch[:, -1, :, prognostic_input_mask] = prognostic_fields
                input_tensor_torch[:, -1, :, computed_forcing_mask] = forcing

                # stepper(i, step)

    # Below are methods forwarded to the checkpoint

    # @cached_property
    # def param_sfc(self):
    #     return self.checkpoint.param_sfc

    # @cached_property
    # def param_level_pl(self):
    #     return self.checkpoint.param_level_pl

    # @cached_property
    # def param_level_ml(self):
    #     return self.checkpoint.param_level_ml

    # @cached_property
    # def grid(self):
    #     return self.checkpoint.grid

    # @cached_property
    # def area(self):
    #     return self.checkpoint.area

    @cached_property
    def hour_steps(self):
        return self.checkpoint.hour_steps

    @cached_property
    def lagged(self):
        result = list(range(0, self.checkpoint.multi_step))
        result = [-s * self.hour_steps for s in result]
        return sorted(result)

    # @cached_property
    # def constant_fields(self):
    #     return self.checkpoint.constants_from_input
