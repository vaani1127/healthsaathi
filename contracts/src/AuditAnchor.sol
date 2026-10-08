// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {ClinicRegistry} from "./ClinicRegistry.sol";

/// @title AuditAnchor
/// @notice Public witness for each clinic's signed Merkle checkpoints. Only tree sizes, roots and
/// the digest of the signed tree head are stored here, never patient data.
contract AuditAnchor {
    struct Head {
        uint64 treeSize;
        bytes32 root;
    }

    ClinicRegistry public immutable registry;
    mapping(bytes32 clinicId => Head) private _latest;

    event Anchored(
        bytes32 indexed clinicId,
        uint64 treeSize,
        bytes32 root,
        bytes32 sthDigest,
        uint256 timestamp
    );

    error NotPoster();
    error TreeSizeNotIncreasing(uint64 last, uint64 given);
    error PrevRootMismatch(bytes32 expected, bytes32 given);

    constructor(ClinicRegistry registry_) {
        registry = registry_;
    }

    /// @param prevRoot Must equal the last anchored root for this clinic (zero for the first).
    function anchor(
        bytes32 clinicId,
        uint64 treeSize,
        bytes32 root,
        bytes32 prevRoot,
        bytes32 sthDigest
    ) external {
        if (msg.sender != registry.posterOf(clinicId)) revert NotPoster();
        Head memory last = _latest[clinicId];
        if (treeSize <= last.treeSize) revert TreeSizeNotIncreasing(last.treeSize, treeSize);
        if (prevRoot != last.root) revert PrevRootMismatch(last.root, prevRoot);

        _latest[clinicId] = Head(treeSize, root);
        emit Anchored(clinicId, treeSize, root, sthDigest, block.timestamp);
    }

    function latest(bytes32 clinicId) external view returns (uint64 treeSize, bytes32 root) {
        Head memory head = _latest[clinicId];
        return (head.treeSize, head.root);
    }
}
