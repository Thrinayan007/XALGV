"""Versioned messages sent between the coordinator and verified task consumers."""

from pydantic import model_validator

from .base import ArtifactManifest, Envelope, Identifier


class ArtifactHandoff(Envelope):
    run_id: Identifier
    producer_task_id: Identifier
    consumer_task_id: Identifier
    input_name: Identifier
    artifact: ArtifactManifest

    @model_validator(mode="after")
    def validate_provenance(self):
        if (self.producer_task_id == self.consumer_task_id
                or self.artifact.path.split("/")[1:3] != [self.run_id, self.producer_task_id]):
            raise ValueError("Handoff artifact must belong to a different task in the same run")
        return self
