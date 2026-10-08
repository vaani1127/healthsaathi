// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {ClinicRegistry} from "./ClinicRegistry.sol";

/// @title ConsentRegistry
/// @notice Optional append-only log of consent commitments (salted hashes only). Not wired into
/// the product yet.
contract ConsentRegistry {
    uint8 public constant GRANTED = 0;
    uint8 public constant WITHDRAWN = 1;

    ClinicRegistry public immutable registry;
    mapping(bytes32 clinicId => uint256) public commitmentCount;

    event ConsentCommitted(
        bytes32 indexed clinicId,
        bytes32 indexed consentHash,
        uint8 kind,
        uint256 index,
        uint256 timestamp
    );

    error NotPoster();
    error InvalidKind(uint8 kind);
    error InvalidHash();

    constructor(ClinicRegistry registry_) {
        registry = registry_;
    }

    function commit(bytes32 clinicId, bytes32 consentHash, uint8 kind) external {
        if (msg.sender != registry.posterOf(clinicId)) revert NotPoster();
        if (kind > WITHDRAWN) revert InvalidKind(kind);
        if (consentHash == bytes32(0)) revert InvalidHash();
        uint256 index = commitmentCount[clinicId]++;
        emit ConsentCommitted(clinicId, consentHash, kind, index, block.timestamp);
    }
}
